"""Strict, nonsecret single-instance policy. Loading a profile never enables models."""
import copy
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from services.managed_resources import DEFAULTS as PROTECTION_DEFAULTS

LOCAL_RESOURCES = dict(start_memory_gib=6, stop_memory_gib=4, disk_gib=5,
                       rss_mib=1536, start_cpu_percent=80)
VISITOR_DEFAULTS = dict(window_seconds=86400, text=0, image=0, credits=0,
    active_tasks=1, service_concurrency=1, queue=2, pages=10, default_auto_pages=5, input_bytes=131072,
    upload_bytes=20971520, storage_bytes=209715200, projects=20, new_visitors_per_hour=60)
VISITOR_DEFAULTS.update(PROTECTION_DEFAULTS)
DEFAULT = dict(version=1, profile='local', mode='preview', data_dir='.copilot',
    secret_file='.env.copilot', public_origin='http://127.0.0.1:5011', port=5011,
    proxy=False, http_threads=8, connection_limit=32, queue=2,
    text_timeout_seconds=720, image_timeout_seconds=180, shutdown_grace_seconds=45,
    resources=LOCAL_RESOURCES, visitor=VISITOR_DEFAULTS, authorization=None)
OPERATIONS = {'text_generate', 'template_generate', 'slide_edit'}


def no_links(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('运行路径不能包含符号链接或目录联接。')
    return path.resolve()


def number(value, low, high, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('运行配置包含缺失或越界数值。')


def authorization(value):
    keys = {'id', 'not_before', 'expires_at', 'credits', 'text', 'image', 'roles', 'operations'}
    if not isinstance(value, dict) or set(value) - {'balance', 'validity'} != keys:
        raise ValueError('live需要完整的运行授权与额度。')
    validity = value.get('validity', 'fixed')
    if validity not in ('fixed', 'daily'):
        raise ValueError('授权期限类型无效。')
    if validity == 'daily' and (value['expires_at'] is not None or not value.get('balance')):
        raise ValueError('日常使用需明确余额基准，结束时间必须为空。')
    if not isinstance(value['id'], str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{7,79}', value['id']):
        raise ValueError('运行授权ID无效。')
    if value.get('balance') is not None:
        from services.manual_balance import validate as validate_balance
        validate_balance(value['balance'])
    # The legacy field can be zero for a new manual-balance configuration.
    # Admin check and send-time guards still enforce balance, prices and counts.
    number(value['credits'], 0 if value.get('balance') else .01, 100000)
    for k in ('text', 'image'): number(value[k], 0, 100000, True)
    if not isinstance(value['roles'], list) or not value['roles'] or set(value['roles']) - {'text', 'image'}:
        raise ValueError('运行角色无效。')
    if not isinstance(value['operations'], list) or set(value['operations']) - OPERATIONS:
        raise ValueError('仅支持已接入追踪的创作操作。')
    for role in ('text', 'image'):
        if (role in value['roles']) != (value[role] > 0): raise ValueError('角色与次数不一致。')
    if ('image' in value['roles']) != bool(value['operations']): raise ValueError('图片操作范围缺失。')
    stamps=[]
    for k in ('not_before', 'expires_at'):
        if k == 'expires_at' and validity == 'daily': continue
        if not isinstance(value[k], str): raise ValueError('授权时间格式无效。')
        t=datetime.fromisoformat(value[k].replace('Z','+00:00'))
        if t.tzinfo is None: raise ValueError('授权时间必须含时区。')
        stamps.append(t.timestamp())
    if validity == 'fixed' and not 0 < stamps[1]-stamps[0] <= 366*86400: raise ValueError('定时授权有效期必须有限且不超过一年。')
    return copy.deepcopy(value)


def authorized_now(value):
    # All entry points share strict validation: null alone never removes expiry.
    authorization(value)
    now=datetime.now(timezone.utc).timestamp()
    started = datetime.fromisoformat(value['not_before'].replace('Z','+00:00')).timestamp() <= now
    return started and (value.get('validity') == 'daily' or now < datetime.fromisoformat(value['expires_at'].replace('Z','+00:00')).timestamp())


def load(path, code_root, *, enforce_authorization=True):
    path=no_links(path); raw=json.loads(path.read_text(encoding='utf8'))
    return validate(raw, code_root, source=path, enforce_authorization=enforce_authorization)


def validate(raw, code_root, *, source, enforce_authorization=True, legacy_page_default=False):
    """Shared validator for CLI bootstrap and the fixed admin-editable projection."""
    if not isinstance(raw, dict) or set(raw)-set(DEFAULT): raise ValueError('未知运行配置字段。')
    cfg={**copy.deepcopy(DEFAULT), **copy.deepcopy(raw)}
    missing_page_default = 'default_auto_pages' not in cfg['visitor']
    cfg['visitor'].setdefault('default_auto_pages', 5)
    for name, value in PROTECTION_DEFAULTS.items():
        cfg['visitor'].setdefault(name, value)
    if cfg['version'] != 1 or cfg['profile'] not in ('local','server') or cfg['mode'] not in ('preview','live'):
        raise ValueError('运行配置版本、profile或mode无效。')
    if type(cfg['proxy']) is not bool: raise ValueError('proxy必须是布尔值。')
    for k,lo,hi in [('port',1024,65535),('http_threads',4,32),('connection_limit',8,128),('queue',0,8),
                    ('text_timeout_seconds',30,1800),('image_timeout_seconds',30,600),('shutdown_grace_seconds',5,1800)]:
        number(cfg[k],lo,hi,True)
    if set(cfg['resources']) != set(LOCAL_RESOURCES): raise ValueError('资源边界必须完整配置。')
    for k,lo,hi in [('start_memory_gib',.25,1024),('stop_memory_gib',.125,1024),('disk_gib',1,10000),('rss_mib',256,65536),('start_cpu_percent',10,95)]:
        number(cfg['resources'][k],lo,hi)
    if cfg['resources']['stop_memory_gib'] > cfg['resources']['start_memory_gib']: raise ValueError('内存停止线高于启动线。')
    if cfg['profile']=='local' and any(cfg['resources'][k]<v for k,v in LOCAL_RESOURCES.items() if k not in ('start_cpu_percent','rss_mib')):
        raise ValueError('不能降低本机资源保护。')
    if cfg['profile']=='local' and cfg['resources']['rss_mib']>1536: raise ValueError('不能降低本机RSS保护。')
    if cfg['profile']=='local' and cfg['resources']['start_cpu_percent']>80: raise ValueError('不能降低本机CPU保护。')
    if set(cfg['visitor']) != set(VISITOR_DEFAULTS): raise ValueError('访客限制必须完整配置。')
    for k,v in cfg['visitor'].items(): number(v,0,2**40,k!='credits')
    v=cfg['visitor']
    for name in ('request_window_seconds', 'visitor_requests', 'global_requests'):
        number(v[name], 1, 86400, True)
    if v['service_storage_bytes'] < v['storage_bytes'] or v['storage_free_bytes'] < 2**30:
        raise ValueError('受管存储总量或磁盘余量边界无效。')
    if v['service_concurrency']!=1 or v['active_tasks']!=1 or v['queue']!=cfg['queue'] or not 60<=v['window_seconds']<=366*86400:
        raise ValueError('仅支持单进程单执行通道、有限排队和有效窗口。')
    if not 1<=v['pages']<=50 or not 1<=v['projects']<=1000 or not 1<=v['new_visitors_per_hour']<=1000:
        raise ValueError('作品或访客数量限制无效。')
    number(v['default_auto_pages'], 1, 50, True)
    if not 1024<=v['input_bytes']<=1048576 or not 1024<=v['upload_bytes']<=20971520 or v['storage_bytes']<v['upload_bytes']:
        raise ValueError('输入、上传或存储边界无效。')
    origin=urlsplit(cfg['public_origin'])
    if origin.username or origin.password or origin.path or origin.query or origin.fragment or not origin.hostname:
        raise ValueError('public_origin必须是明确的同源地址。')
    if cfg['proxy']:
        if cfg['profile']!='server' or origin.scheme!='https' or origin.port not in (None,443): raise ValueError('仅服务器HTTPS使用单层受信代理。')
        if origin.hostname.endswith('.invalid') or 'REPLACE' in cfg['public_origin']: raise ValueError('部署域名尚未填写。')
    elif cfg['public_origin']!=f'http://127.0.0.1:{cfg["port"]}': raise ValueError('直连只允许明确的环回地址。')
    for name in ('data_dir','secret_file'):
        p=Path(cfg[name]);p=no_links(p if p.is_absolute() else Path(code_root)/p)
        if cfg['profile']=='local' and not p.is_relative_to(Path(code_root).resolve()): raise ValueError('本机路径必须留在项目内。')
        cfg[name]=str(p)
    if Path(cfg['secret_file']).name!='.env.copilot': raise ValueError('唯一秘密文件名为.env.copilot。')
    if cfg['mode']=='live':
        cfg['authorization']=authorization(cfg['authorization'])
        if enforce_authorization and not authorized_now(cfg['authorization']): raise ValueError('运行授权未生效或已过期。')
        if not v['credits'] and not cfg['authorization'].get('balance'): raise ValueError('live访客额度尚未开放。')
    elif cfg['authorization'] is not None or any(v[k] for k in ('credits','image','text')):
        raise ValueError('preview示例不得携带生效的模型额度。')
    cfg['_source']=str(source)
    return cfg
