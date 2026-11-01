"""Thin CoreX client. No provider credentials, generation engine or wallet logic."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit
import uuid

import httpx
import jsonschema
from .private import ensure_private_directory

DEFAULT_CREDENTIALS = (Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'CoreX/mcp/credentials.json'
                       if os.name == 'nt' else Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'corex-mcp/credentials.json')
UGC_TOOLS = {'get_integration_context', 'list_models', 'list_characters', 'list_assets', 'get_asset', 'add_asset',
             'prepare_ugc_campaign', 'quote_ugc_video', 'generate_ugc_video', 'get_ugc_operation', 'cancel_ugc_operation',
             'get_job', 'quote_ugc_captions', 'generate_ugc_captions'}
MIME = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp','.gif':'image/gif','.avif':'image/avif',
        '.mp4':'video/mp4','.mov':'video/quicktime','.webm':'video/webm','.mkv':'video/x-matroska',
        '.mp3':'audio/mpeg','.wav':'audio/wav','.m4a':'audio/mp4','.aac':'audio/aac','.ogg':'audio/ogg','.weba':'audio/webm'}
UPLOAD_TOOL = {'name':'upload_file','description':'Upload one explicitly selected local media file from the configured upload_root. Requires user confirmation. Returns a key for add_asset; does not register or generate automatically.',
    'inputSchema':{'type':'object','properties':{'file_path':{'type':'string','minLength':1},'confirmed':{'const':True,'type':'boolean'}},
                   'required':['file_path','confirmed'],'additionalProperties':False},'read_only':False,'destructive':False}


def credential_path(explicit=None):
    return Path(explicit or os.environ.get('COREX_CREDENTIALS_FILE') or DEFAULT_CREDENTIALS).expanduser()


def catalog():
    return json.loads(files('corex_mcp').joinpath('catalog.json').read_text(encoding='utf-8'))['tools']


def sanitize(value, token=''):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if str(k).lower() in {'authorization','token','password','secret','api_key','access_token','refresh_token','tokenhash'}
                else sanitize(v,token) for k,v in value.items()}
    if isinstance(value,list):return [sanitize(v,token) for v in value]
    if isinstance(value,str):
        if token:value=value.replace(token,'[REDACTED]')
        return re.sub(r'corex_(?:ag|ugc|studio)_[A-Za-z0-9_-]+','[REDACTED]',value)
    return value


class CoreXError(Exception):
    def __init__(self, code, message, *, uncertain=False):
        super().__init__(message)
        self.code,self.message,self.uncertain=code,message,uncertain
    def result(self):
        data={'code':self.code,'message':self.message,'outcome_uncertain':self.uncertain}
        return {'isError':True,'structuredContent':data,'content':[{'type':'text','text':json.dumps(data)}]}


class CoreX:
    MAX_RESPONSE=8*1024*1024
    MAX_UPLOAD=500*1024*1024
    def __init__(self, *, credentials=None, base=None, token=None, upload_root=None, transport=None):
        self.credentials=credential_path(credentials)
        saved=json.loads(self.credentials.read_text(encoding='utf-8-sig')) if self.credentials.is_file() else {}
        selected=base or saved.get('base_url') or 'https://corex.art'
        if not isinstance(selected,str):raise CoreXError('configuration','Invalid CoreX origin.')
        self.base=selected.rstrip('/')
        try:
            url=urlsplit(self.base);url.port
        except (ValueError,TypeError):raise CoreXError('configuration','Invalid CoreX origin.') from None
        local=url.scheme=='http' and url.hostname in {'127.0.0.1','localhost'}
        if not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/') or not (url.scheme=='https' or local):
            raise CoreXError('configuration','Use a HTTPS origin, or loopback HTTP for isolated tests.')
        self.token=token or saved.get('token') or ''
        if not isinstance(self.token,str) or not re.fullmatch(r'corex_(?:studio|ugc)_[A-Za-z0-9_-]{43}',self.token):
            raise CoreXError('credentials','Configure a workspace integration token; operator tokens and JWTs are not accepted.')
        self.upload_root=Path(upload_root or saved['upload_root']).expanduser() if upload_root or saved.get('upload_root') else None
        self.operations={t['name']:t for t in catalog() if not self.token.startswith('corex_ugc_') or t['name'] in UGC_TOOLS}
        self.operations['upload_file']=UPLOAD_TOOL
        self.http=httpx.Client(base_url=self.base,follow_redirects=False,trust_env=False,
                               timeout=httpx.Timeout(60,connect=10,write=120),transport=transport)

    def close(self):self.http.close()

    def _request(self, path, *, uncertain=True, **kwargs):
        headers={'Authorization':'Bearer '+self.token,'Accept':'application/json','MCP-Protocol-Version':'2025-06-18'}
        try:
            with self.http.stream('POST',path,headers=headers,**kwargs) as response:
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>self.MAX_RESPONSE:raise CoreXError('response_limit','CoreX response exceeded the client limit.',uncertain=uncertain)
                if response.status_code!=200 and response.status_code!=201:
                    raise CoreXError('http_'+str(response.status_code),'CoreX rejected the request. Check status before repeating it.',
                                     uncertain=uncertain and (response.status_code>=500 or 300<=response.status_code<400))
                try:return json.loads(raw)
                except (ValueError,UnicodeError):raise CoreXError('invalid_response','CoreX returned an invalid response.',uncertain=uncertain) from None
        except httpx.HTTPError:
            raise CoreXError('transport','Connection failed. Inspect the server state before retrying a write.',uncertain=uncertain) from None

    def rpc(self, method, params=None, *, uncertain=False):
        request_id=uuid.uuid4().hex
        data=self._request('/api/studio/mcp',json={'jsonrpc':'2.0','id':request_id,'method':method,'params':params or {}},uncertain=uncertain)
        if not isinstance(data,dict) or data.get('jsonrpc')!='2.0' or data.get('id')!=request_id:
            raise CoreXError('protocol','Unexpected CoreX response identity.',uncertain=uncertain)
        if 'error' in data:raise CoreXError('rpc','CoreX rejected the operation; inspect the current contract.')
        if not isinstance(data.get('result'),dict):raise CoreXError('protocol','CoreX result is missing.',uncertain=uncertain)
        return sanitize(data['result'],self.token)

    def call(self, name, arguments):
        operation=self.operations.get(name)
        if operation is None:raise CoreXError('tool','Tool is unavailable for this integration scope.')
        try:jsonschema.Draft202012Validator(operation['inputSchema']).validate(arguments)
        except jsonschema.ValidationError as error:
            raise CoreXError('arguments','Invalid input at '+'.'.join(map(str,error.absolute_path))) from None
        if name=='upload_file':return self.upload(arguments['file_path'])
        result=self.rpc('tools/call',{'name':name,'arguments':arguments},uncertain=not operation['read_only'])
        if not isinstance(result.get('content'),list):raise CoreXError('protocol','Missing MCP result content.',uncertain=not operation['read_only'])
        return result

    def status(self):
        result=self.rpc('tools/list')
        names={t.get('name') for t in result.get('tools',[]) if isinstance(t,dict)}
        expected=set(self.operations)-{'upload_file'}
        return {'remote_tools':len(names),'missing_tools':sorted(expected-names),'upload_enabled':self.upload_root is not None}

    @contextmanager
    def _snapshot(self, filename):
        if self.upload_root is None:raise CoreXError('upload_root','Set upload_root in your private configuration before uploading.')
        root=self.upload_root
        path=Path(filename).expanduser()
        if not root.is_absolute() or not path.is_absolute() or '..' in path.parts or str(path).startswith('\\\\'):
            raise CoreXError('file','Use an absolute media path inside upload_root.')
        try:
            root=root.resolve(strict=True)
            path.relative_to(root)
            for item in (path,*path.parents):
                if item==root:break
                if item.is_symlink() or getattr(item,'is_junction',lambda:False)():raise CoreXError('file','Links are not accepted for uploads.')
            if path.resolve(strict=True)!=path or path.suffix.lower() not in MIME or any(p.startswith('.') or p.lower() in {'secrets','credentials','profiles'} for p in path.relative_to(root).parts):
                raise CoreXError('file','Only regular media files inside upload_root may be uploaded.')
            before=path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink>1 or not 0<before.st_size<=self.MAX_UPLOAD:
                raise CoreXError('file','Media file is empty, linked, or exceeds 500 MiB.')
        except (ValueError,OSError):raise CoreXError('file','Media path is outside upload_root or unavailable.') from None
        cache=ensure_private_directory(self.credentials.parent/'upload-cache')
        with tempfile.TemporaryDirectory(prefix='snapshot-',dir=cache) as directory:
            copy=Path(directory)/'media.bin';digest=hashlib.sha256();size=0
            with path.open('rb') as source,copy.open('xb') as target:
                current=os.fstat(source.fileno())
                if (current.st_dev,current.st_ino,current.st_size)!=(before.st_dev,before.st_ino,before.st_size):
                    raise CoreXError('file_changed','Media changed before copying; nothing was sent.')
                while chunk:=source.read(1024*1024):
                    size+=len(chunk)
                    if size>self.MAX_UPLOAD:raise CoreXError('file_changed','Media changed size; nothing was sent.')
                    digest.update(chunk);target.write(chunk)
                after=os.fstat(source.fileno())
                if after.st_mtime_ns!=before.st_mtime_ns or size!=before.st_size:
                    raise CoreXError('file_changed','Media changed during copying; nothing was sent.')
            with copy.open('rb') as snapshot:
                header=snapshot.read(64);snapshot.seek(0)
                ext=path.suffix.lower()
                valid=(header.startswith(b'\x89PNG\r\n\x1a\n') if ext=='.png' else
                       header.startswith(b'\xff\xd8\xff') if ext in {'.jpg','.jpeg'} else
                       header.startswith((b'GIF87a',b'GIF89a')) if ext=='.gif' else
                       header.startswith(b'RIFF') and header[8:12]==b'WEBP' if ext=='.webp' else
                       header.startswith(b'RIFF') and header[8:12]==b'WAVE' if ext=='.wav' else
                       header[4:8]==b'ftyp' if ext in {'.mp4','.mov','.m4a','.avif'} else
                       header.startswith(b'\x1aE\xdf\xa3') if ext in {'.webm','.mkv','.weba'} else
                       header.startswith(b'OggS') if ext=='.ogg' else
                       header.startswith(b'ID3') or len(header)>1 and header[0]==255 and header[1]&224==224)
                if not valid:raise CoreXError('media','File content does not match supported media; nothing was sent.')
                yield snapshot,path.name,MIME[ext],size,digest.hexdigest()

    def upload(self, filename):
        with self._snapshot(filename) as (snapshot,name,mime,size,digest):
            result=self._request('/api/studio/uploads',files={'file':(name,snapshot,mime)})
        if not isinstance(result,dict) or not isinstance(result.get('key'),str) or not result['key']:
            raise CoreXError('upload_response','Upload result is incomplete; inspect the server before retrying.',uncertain=True)
        data=sanitize({**result,'sha256':digest,'bytes':size,'registered':False,'next_tool':'add_asset'},self.token)
        return {'isError':False,'structuredContent':data,'content':[{'type':'text','text':json.dumps(data)}]}
