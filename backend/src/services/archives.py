"""分发归档的逐份核对与原位修复。

归档由两份文件组成：明细 CSV 与调查说明 JSON（归档时保存的任务名称、参数和结果快照）。
核对与修复全部在服务端完成，前端只展示服务端结论。
"""
import os, io, csv, json, hashlib, tempfile, fcntl
from contextlib import contextmanager
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.config.settings import ARTIFACT_DIR
from src.models import Delivery, ArchiveRepair
from src.services.audit import record

NOTE_KEYS = ('job', 'params', 'result', 'checksum')


def _safe(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    text = str(v)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def csv_bytes(result):
    """由结果快照生成归档明细 CSV；分发与修复共用同一渲染口径。"""
    out = io.StringIO()
    out.write('﻿')
    table = (result or {}).get('table', {})
    cols = table.get('columns', [])
    writer = csv.writer(out)
    writer.writerow([_safe(c['label']) for c in cols])
    for row in table.get('rows', []):
        writer.writerow([_safe(row.get(c['key'], '')) for c in cols])
    return out.getvalue().encode('utf-8')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(job, checksum_value):
    """归档时保存的调查及参数快照；后续修复只认这份快照。"""
    return {'job': job.name, 'params': job.params, 'result': job.result, 'checksum': checksum_value}


def _paths(delivery):
    if not delivery or not delivery.filename:
        return None, None
    base = os.path.join(ARTIFACT_DIR, delivery.filename)
    return base, base + '.json'


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.repair-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def archive_lock(delivery_id):
    """跨进程串行化同一份归档的修复；数据库行锁在 SQLite 下不生效，文件锁兜底。"""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    lock_path = os.path.join(ARTIFACT_DIR, f'.repair-{delivery_id}.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_note(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _inspect_detail(delivery, path):
    if path is None:
        return {'state': 'missing', 'reason': '归档未记录明细文件名，无法定位明细文件'}
    if not os.path.exists(path):
        return {'state': 'missing', 'reason': '明细文件缺失'}
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return {'state': 'damaged', 'reason': '明细文件为空或不是普通文件'}
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        return {'state': 'damaged', 'reason': '明细文件无法读取：' + str(e)}
    if sha256(data) != delivery.checksum:
        return {'state': 'damaged', 'reason': '明细文件校验值与归档记录不一致'}
    return {'state': 'ok', 'reason': '明细文件存在且校验值一致'}


def _inspect_note(delivery, path):
    """校验调查说明；只有同时满足结构与校验值连续性才可作为恢复依据。"""
    if path is None:
        return {'state': 'missing', 'reason': '归档未记录明细文件名，无法定位调查说明'}, None
    if not os.path.exists(path):
        return {'state': 'missing', 'reason': '调查说明缺失'}, None
    try:
        note = _read_note(path)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return {'state': 'damaged', 'reason': '调查说明无法解析：' + str(e)[:200]}, None
    if not isinstance(note, dict):
        return {'state': 'damaged', 'reason': '调查说明不是有效对象'}, None
    missing = [k for k in NOTE_KEYS if k not in note]
    if missing:
        return {'state': 'damaged', 'reason': '调查说明缺少恢复字段：' + '、'.join(missing)}, None
    if not isinstance(note.get('result'), dict):
        return {'state': 'damaged', 'reason': '调查说明缺少可恢复的结果快照'}, None
    table = note['result'].get('table')
    if not isinstance(table, dict) or not isinstance(table.get('columns'), list) or not isinstance(table.get('rows'), list):
        return {'state': 'damaged', 'reason': '调查说明中的结果明细结构不完整'}, None
    if not note['checksum'] or note['checksum'] != delivery.checksum:
        return {'state': 'damaged', 'reason': '调查说明校验值与归档记录不一致'}, None
    try:
        rendered = csv_bytes(note['result'])
    except Exception as e:
        return {'state': 'damaged', 'reason': '调查说明无法还原明细：' + str(e)[:200]}, None
    if sha256(rendered) != delivery.checksum:
        return {'state': 'damaged', 'reason': '调查说明与归档明细校验值不一致，不能作为恢复依据'}, None
    return {'state': 'ok', 'reason': '调查说明存在且与归档校验值一致'}, note


def check(delivery):
    """逐份核对明细文件与调查说明，并给出是否可原位修复。"""
    detail_path, note_path = _paths(delivery)
    detail = _inspect_detail(delivery, detail_path)
    note, note_data = _inspect_note(delivery, note_path)
    intact = detail['state'] == 'ok' and note['state'] == 'ok'
    repairable = note['state'] == 'ok' and detail['state'] != 'ok'
    if intact:
        status, reason = 'intact', '明细文件与调查说明齐全，校验值一致'
    elif repairable:
        detail_word = '缺失' if detail['state'] == 'missing' else '损坏'
        status, reason = 'repairable', '明细文件' + detail_word + '：' + detail['reason'] + '；可由归档时保存的调查及参数原位恢复'
    else:
        reasons = []
        if detail['state'] != 'ok':
            reasons.append('明细文件' + ('缺失' if detail['state'] == 'missing' else '损坏') + '：' + detail['reason'])
        if note['state'] != 'ok':
            reasons.append('缺少可信恢复依据：调查说明' + ('缺失' if note['state'] == 'missing' else '损坏') + '（' + note['reason'] + '）')
        if detail['state'] == 'ok':
            reasons.append('无法确认归档完整性，已停止下载')
        status, reason = 'blocked', '；'.join(reasons)
    return {'delivery_id': delivery.id, 'filename': delivery.filename, 'destination': delivery.destination,
            'checksum': delivery.checksum, 'created_at': delivery.created_at, 'detail': detail, 'note': note,
            'status': status, 'repairable': repairable, 'reason': reason, 'source': 'archived_snapshot' if repairable else ''}


def _repair_record(db, delivery_id):
    return db.scalar(select(ArchiveRepair).where(ArchiveRepair.delivery_id == delivery_id).with_for_update())


def _save_repair(db, delivery_id, status, reason, actor, source=''):
    import time
    now = time.time()
    row = _repair_record(db, delivery_id)
    if row is None:
        # unique(delivery_id) 兜底：并发修复时只有一份记录能落库，另一份转为更新。
        try:
            with db.begin_nested():
                row = ArchiveRepair(delivery_id=delivery_id, status=status, reason=reason[:990], source=source,
                                    actor=actor, checked_at=now, finished_at=now)
                db.add(row)
                db.flush()
            return row
        except IntegrityError:
            row = _repair_record(db, delivery_id)
            if row is None:
                raise
    row.status = status
    row.reason = reason[:990]
    row.source = source
    row.actor = actor
    row.checked_at = now
    row.finished_at = now
    db.flush()
    return row


def repair(db, delivery_id, actor):
    """原位修复一份归档：沿用原编号、接收组与校验值，重试幂等，不新增归档记录。

    同一份归档的修复在数据库行锁与文件锁下串行执行；后到者会重新核对，
    发现已完好即跳过，因此并发或重试都只生效一次。
    """
    with archive_lock(delivery_id):
        db.expire_all()
        return _repair_locked(db, delivery_id, actor)


def _repair_locked(db, delivery_id, actor):
    # 行锁在支持它的数据库上把同一份归档的并发修复串行化（MySQL）。
    delivery = db.scalar(select(Delivery).where(Delivery.id == delivery_id).with_for_update())
    if delivery is None:
        return {'delivery_id': delivery_id, 'outcome': 'missing', 'reason': '归档不存在', 'repair': None, 'check': None}
    existing = _repair_record(db, delivery_id)
    current = check(delivery)
    if current['status'] == 'intact':
        outcome = 'already_repaired' if existing and existing.status == 'repaired' else 'already_intact'
        return {'delivery_id': delivery_id, 'outcome': outcome, 'reason': current['reason'],
                'repair': {'status': existing.status, 'finished_at': existing.finished_at} if existing else None,
                'check': current}
    if not current['repairable']:
        row = _save_repair(db, delivery_id, 'refused', current['reason'], actor)
        record(db, actor, 'archive.repair_refused', {'delivery': delivery_id, 'reason': current['reason']})
        return {'delivery_id': delivery_id, 'outcome': 'refused', 'reason': current['reason'],
                'repair': {'status': row.status, 'finished_at': row.finished_at}, 'check': current}
    detail_path, note_path = _paths(delivery)
    try:
        note = _read_note(note_path)
        data = csv_bytes(note['result'])
        actual = sha256(data)
        if actual != delivery.checksum:
            raise ValueError('恢复快照与归档校验值不一致')
        if os.path.exists(detail_path):
            # 修复前保留损坏现场，必要时才留一份副本；不删除任何证据。
            backup = detail_path + '.damaged'
            if not os.path.exists(backup):
                with open(detail_path, 'rb') as src, open(backup, 'wb') as dst:
                    dst.write(src.read())
        _atomic_write(detail_path, data)
        after = check(delivery)
        if after['status'] != 'intact':
            raise ValueError('修复后复核未通过：' + after['reason'])
        row = _save_repair(db, delivery_id, 'repaired', '明细文件已按归档时保存的调查及参数原位恢复', actor, 'archived_snapshot')
        record(db, actor, 'archive.repair', {'delivery': delivery_id, 'checksum': delivery.checksum,
                                             'destination': delivery.destination, 'source': 'archived_snapshot'})
        return {'delivery_id': delivery_id, 'outcome': 'repaired', 'reason': '明细文件已按归档时保存的调查及参数原位恢复',
                'repair': {'status': row.status, 'finished_at': row.finished_at}, 'check': after}
    except Exception as e:
        reason = '修复未完成：' + str(e)[:300]
        row = _save_repair(db, delivery_id, 'failed', reason, actor)
        record(db, actor, 'archive.repair_failed', {'delivery': delivery_id, 'reason': reason})
        return {'delivery_id': delivery_id, 'outcome': 'failed', 'reason': reason,
                'repair': {'status': row.status, 'finished_at': row.finished_at}, 'check': check(delivery)}
