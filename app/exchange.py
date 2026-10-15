"""Authenticated encrypted job inputs and artifacts for public Actions repos."""
import base64
import hmac
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def seal(token, obj, identity):
    raw=json.dumps(obj,separators=(',',':')).encode()
    if len(raw)>131072:raise ValueError('Envelope too large')
    nonce=os.urandom(12)
    key=hmac.digest(token.encode(),b'bot-actions-envelope-v1','sha256')
    encrypted=AESGCM(key).encrypt(nonce,raw,identity.encode())
    return 'v1.'+base64.urlsafe_b64encode(nonce+encrypted).decode().rstrip('=')


def unseal(token, value, identity):
    if not isinstance(value,str) or not value.startswith('v1.') or len(value)>180000:
        raise ValueError('Invalid encrypted envelope')
    raw=base64.urlsafe_b64decode(value[3:]+'='*(-len(value[3:])%4))
    key=hmac.digest(token.encode(),b'bot-actions-envelope-v1','sha256')
    obj=json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],identity.encode()))
    if not isinstance(obj,dict) or obj.get('id')!=identity:raise ValueError('Envelope identity mismatch')
    return obj


def load_job_env():
    if not os.getenv('JOB_PAYLOAD'):return
    obj=unseal(os.environ['TELEGRAM_BOT_TOKEN'],os.environ['JOB_PAYLOAD'],os.environ['JOB_ID'])
    fields={'owner':'JOB_OWNER','chat':'JOB_CHAT','message_id':'JOB_MESSAGE_ID','url':'JOB_SOURCE_URL',
        'requested_name':'JOB_REQUESTED_NAME','partitions':'JOB_PARTITIONS',
        'thread_id':'JOB_THREAD_ID','request_message_id':'JOB_REPLY_TO'}
    for name,env in fields.items():
        if name in obj:os.environ[env]=str(obj[name])


def write_result(path, result, token):
    output={'id':result['id'],'state':result['state'],'sealed':seal(token,result,result['id'])}
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(output),encoding='utf-8')
