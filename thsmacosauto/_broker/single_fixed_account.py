"""Rust r4:v1 frozen policy consumer. No HTTP admission, persistence or adoption.

The incumbent schema has no expiry/version field; its domain tag versions the key.
Extra JSON fields stay inert as in Rust. No new expiry or naming normalization.
Raw transport scopes remain private memory, never funding identifiers.
"""
import hashlib
import json
import re
from types import MappingProxyType
from .private_policy_read import read_private, require

IDENTITY_KIND='USER_CONFIRMED_SINGLE_ACCOUNT'
BROKER='天府证券'

def digest(domain,value):
    return hashlib.sha256(domain.encode()+b'\0'+value.encode()).hexdigest()

def strict_policy(raw):
    # Bound depth before json's recursive decoder; ignore brackets in strings.
    depth=0;quoted=False;escaped=False
    for ch in raw.decode('utf-8'):
        if quoted:
            if escaped:escaped=False
            elif ch=='\\':escaped=True
            elif ch=='"':quoted=False
        elif ch=='"':quoted=True
        elif ch in '[{':
            depth+=1;require(depth<=16,'POLICY_DEPTH')
        elif ch in ']}':depth-=1
    def pairs(items):
        out={}
        for key,value in items:
            require(key not in out,'POLICY_DUPLICATE_KEY');out[key]=value
        return out
    def invalid(_):raise ValueError('POLICY_NONFINITE')
    value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid)
    require(type(value) is dict,'POLICY_OBJECT')
    return value

class SingleFixedAccount:
    __slots__=('_raw','_scopes','_key')
    def __init__(self,*_):raise TypeError('PRIVATE_POLICY_REQUIRED')
    @classmethod
    def from_private_policy(cls,path):
        try:
            v=strict_policy(read_private(path,16384))
            require(v.get('account_identity_kind')==IDENTITY_KIND and v.get('broker')==BROKER,'IDENTITY_CHANGED')
            raw={}
            for market,key in [('深圳','raw_shenzhen_transport_scope'),('上海','raw_shanghai_transport_scope')]:
                s=v.get(key)
                # Rust as_str() ignores absent/non-string optional Shanghai.
                if type(s) is str:
                    require(re.fullmatch(r'[A-Za-z0-9-]+',s) is not None,'SCOPE_UNAVAILABLE');raw[market]=s
            require('深圳' in raw,'SCOPE_UNAVAILABLE')
            scopes={m:digest('ths:shareholder',s) for m,s in sorted(raw.items())}
            material=json.dumps([BROKER,scopes],ensure_ascii=False,separators=(',',':'))
            key=digest('ths:single-fixed-account:r4:v1',material)
            require(v.get('account_key')==key and v.get('scope_hashes')==scopes,'IDENTITY_CHANGED')
            obj=object.__new__(cls);obj._raw=MappingProxyType(raw);obj._scopes=MappingProxyType(scopes);obj._key=key
            return obj
        except (OSError,ValueError,TypeError,RecursionError,UnicodeError):
            raise ValueError('FIXED_POLICY_UNAVAILABLE') from None
    def account_key(self):return self._key
    def transport_scopes(self):return dict(self._raw)
    def observation_status(self,broker,scopes):
        observed=dict(scopes)
        missing=len(set(self._raw)-set(observed))
        if broker!=BROKER or any(m not in self._raw or self._raw[m]!=v for m,v in observed.items()):
            return 'FIXED_SCOPE_CHANGED',missing
        return ('FIXED_SCOPE_INCOMPLETE_OBSERVATION' if missing else 'FIXED_SCOPE_COMPLETE_OBSERVATION'),missing
    def verify_visible(self,broker,scopes):
        status,_=self.observation_status(broker,scopes)
        require(status=='FIXED_SCOPE_COMPLETE_OBSERVATION',status)
        return self._key
