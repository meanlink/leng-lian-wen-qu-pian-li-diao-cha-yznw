import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import Report, Delivery, ArchiveRepair
from src.schemas.report import ReportInput, ArchiveRepairInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports, archives
from src.services.audit import record
from src.utils.serialization import report_dict, delivery_dict
router=APIRouter()
@router.get('/reports')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [report_dict(r) for r in db.scalars(select(Report).order_by(Report.id.desc()))]
@router.post('/reports',status_code=201)
def create(data:ReportInput,db=Depends(get_db),u=Depends(admin_only)):
    r=reports.create(db,data); record(db,u.email,'report.schedule',{'id':r.id,'destination':r.destination}); db.commit()
    return report_dict(r)
@router.post('/reports/{id}/toggle')
def toggle(id:int,db=Depends(get_db),u=Depends(admin_only)):
    r=db.get(Report,id)
    if not r:raise HTTPException(404,'分发计划不存在')
    r.enabled=not r.enabled;record(db,u.email,'report.toggle',{'id':id,'enabled':r.enabled});db.commit()
    return report_dict(r)
@router.get('/deliveries')
def deliveries(db=Depends(get_db),u=Depends(current_user)):
    return [delivery_dict(d) for d in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]
@router.get('/deliveries/checks')
def delivery_checks(db=Depends(get_db),u=Depends(current_user)):
    """逐份核对收件箱：明细文件是否可下载、调查说明能否支撑原位修复。"""
    rows=db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(500)).all()
    repairs={r.delivery_id:r for r in db.scalars(select(ArchiveRepair)).all()}
    result=[]
    for d in rows:
        item=archives.check(d)
        rep=repairs.get(d.id)
        item['repair_status']=rep.status if rep else ''
        item['repair_reason']=rep.reason if rep else ''
        item['repair_finished_at']=rep.finished_at if rep else None
        result.append(item)
    return result
@router.post('/deliveries/repair')
def repair_deliveries(data:ArchiveRepairInput,db=Depends(get_db),u=Depends(admin_only)):
    """按选择的归档顺序逐份修复；已完好或已修复的档案直接跳过，中断后可再次提交续跑。"""
    ids=list(dict.fromkeys(data.delivery_ids))
    items=[]
    for delivery_id in ids:
        outcome=archives.repair(db,delivery_id,u.email)
        items.append(outcome)
        db.commit()
    repaired=[i for i in items if i['outcome']=='repaired']
    untouched=[i for i in items if i['outcome'] in ('already_intact','already_repaired')]
    refused=[i for i in items if i['outcome'] in ('refused','missing')]
    failed=[i for i in items if i['outcome']=='failed']
    return {'items':items,'repaired':len(repaired),'untouched':len(untouched),'refused':len(refused),'failed':len(failed)}
@router.get('/deliveries/{id}/download')
def download(id:int,format:str='csv',db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Delivery,id)
    if not d:raise HTTPException(404,'归档不存在')
    if format not in ('csv','json'):raise HTTPException(422,'支持 CSV 或 JSON')
    checked=archives.check(d)
    if checked['status']!='intact':
        raise HTTPException(409,'归档损坏，已停止下载：'+checked['reason']+'。请在页面选择待修档案执行原位修复。')
    filename=d.filename+('.json' if format=='json' else '')
    p=os.path.join(ARTIFACT_DIR,filename)
    if not os.path.exists(p):raise HTTPException(409,'归档文件缺失，已停止下载。请在页面选择待修档案执行原位修复。')
    return FileResponse(p,filename=filename,media_type='application/json' if format=='json' else 'text/csv; charset=utf-8')
