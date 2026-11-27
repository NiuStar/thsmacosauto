"""Explicit same-owner authentication; native AX digits only, no OCR or pixels.

Secret-file admission is retained from the R24 donor. No editable AXValue reads.
UNKNOWN is a durable human-reset fence; only verified success permits a new
explicit request after a fresh logged-out entry/form proof and a cooldown.
"""
from pathlib import Path
import json, math, os, re, stat, time
from . import readonly_budget as rb

SUCCESS_COOLDOWN_NS=1_000_000_000
class LoginFailure(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)

def read_password(filename):
    try:
        from .private_policy_read import literal, read_private
        from .server import strict_json
        data=strict_json(read_private(literal(filename), limit=4096))
        if set(data)!={'schema','password'} or data['schema']!='THS_LOGIN_SECRET_V1':raise ValueError()
        secret=data['password']
        if type(secret) is not str or not secret or len(secret)>256 or '\x00' in secret:raise ValueError()
        return secret
    except Exception:raise LoginFailure('CREDENTIAL_REQUIRED') from None

def geometry(owner,ref):
    p=owner._get(ref,'AXPosition');s=owner._get(ref,'AXSize')
    if not isinstance(p,dict) or not isinstance(s,dict):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    v=(p.get('x'),p.get('y'),s.get('width'),s.get('height'))
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in v) or min(v[2:])<=0:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    return v

def discover(owner,app,root):
    """Fresh unique sheet; sheet index is deliberately not an authority.

    Read labels only on indices 10/14/22/23 after structural validation. All
    other account/static text values are opaque, including combo contents.
    """
    get=owner._get;key=app.backend.key
    windows=get(app._root,'AXWindows') or []
    if not isinstance(windows,(list,tuple)) or len(windows)>12:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    window_keys=[key(w) for w in windows]; root_key=key(root)
    if len(set(window_keys))!=len(window_keys) or root_key not in window_keys:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    queue=[(w,k,0) for w,k in zip(windows,window_keys)];seen={};sheets=[]
    from .login_result import MODAL_CONTAINERS, is_modal
    while queue:
        ref,window_key,depth=queue.pop(0);k=key(ref)
        if k in seen:
            if seen[k]!=window_key:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
            continue
        seen[k]=window_key
        if len(seen)>600 or depth>20:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        role=get(ref,'AXRole');sub=get(ref,'AXSubrole')
        if sub=='AXSecureTextField' or role in ('AXTextField','AXSecureTextField','AXComboBox','AXTextArea'):continue
        if (not depth and role!='AXWindow') or (depth and role=='AXWindow'):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        if role=='AXSheet':
            if window_key!=root_key or get(ref,'AXIdentifier')!='_NS:259':raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
            sheets.append((ref,window_key))
        if role not in MODAL_CONTAINERS:continue
        # Some modal dialogs are exposed as windows/groups, not sheets.
        if role!='AXSheet' and is_modal(get,ref,role,sub):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        children=get(ref,'AXChildren') or []
        extra=(get(ref,'AXSheets') or []) if role in ('AXWindow','AXSheet') else []
        if not isinstance(children,(list,tuple)) or not isinstance(extra,(list,tuple)) or len(children)+len(extra)>600:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        queue.extend((child,window_key,depth+1) for child in (*children,*extra))
    if len(sheets)!=1:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    sheet,window_key=sheets[0]
    if window_key!=root_key:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    if get(sheet,'AXRole')!='AXSheet' or get(sheet,'AXIdentifier')!='_NS:259':raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    parent=get(sheet,'AXParent')
    if parent is None or key(parent)!=key(root) or get(root,'AXRole')!='AXWindow':raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    # Require the forward edge too; a sibling sheet cannot qualify through a
    # stale parent pointer. AXChildren and AXSheets can expose the same sheet.
    direct=get(root,'AXChildren') or []; attached=get(root,'AXSheets') or []
    if (not isinstance(direct,(list,tuple)) or not isinstance(attached,(list,tuple))
            or len(direct)+len(attached)>600
            or all(key(ref)!=key(sheet) for ref in (*direct,*attached))):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    sx,sy,sw,sh=geometry(owner,sheet)
    if abs(sw-453)>2 or abs(sh-292)>2:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    children=get(sheet,'AXChildren') or []
    if not isinstance(children,(list,tuple)) or len(children)!=29 or len({key(c) for c in children})!=29:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    expected={2:'AXComboBox',4:'AXComboBox',6:'AXComboBox',8:'AXComboBox',10:'AXStaticText',11:'AXTextField',13:'AXComboBox',14:'AXStaticText',15:'AXTextField',16:'AXButton',22:'AXButton',23:'AXButton'}
    expected.update({i:'AXStaticText' for i in range(17,22)})
    nodes=[]
    for i,ref in enumerate(children):
        role=get(ref,'AXRole');sub=get(ref,'AXSubrole')
        if i in expected and role!=expected[i]:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        if (sub=='AXSecureTextField' or role=='AXSecureTextField')!=(i==11):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        if get(ref,'AXParent') is None or key(get(ref,'AXParent'))!=key(sheet):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        nodes.append((key(ref),role,sub))
    specs={10:(217,139,52,25),11:(277,139,154,25),14:(217,202,52,25),15:(277,202,90,25),16:(372,203,60,24),22:(277,240,154,32),23:(237,240,42,32)}
    bounds={}
    for i,want in specs.items():
        x,y,w,h=geometry(owner,children[i]);g=(x-sx,y-sy,w,h)
        if any(abs(a-b)>2 for a,b in zip(g,want)):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
        bounds[i]=g
    for i,label,attr in ((10,'交易密码','AXValue'),(14,'验证码','AXValue'),(22,'登录','AXTitle'),(23,'取消','AXTitle')):
        if get(children[i],attr)!=label:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    bx,by,bw,bh=geometry(owner,children[16]);last=bx
    for i in range(17,22):
        x,y,w,h=geometry(owner,children[i])
        if not (last<=x and bx<=x and x+w<=bx+bw+2 and abs(y-by)<=2 and abs(h-bh)<=2):raise LoginFailure('CHALLENGE_UNAVAILABLE')
        last=x+w;bounds[i]=(x-sx,y-sy,w,h)
    for i in (11,15,22):
        if get(children[i],'AXEnabled') is not True:raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    for i in (11,15):
        if not app.backend.is_settable(children[i],'AXValue'):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    if 'AXPress' not in app.backend.actions(children[22]):raise LoginFailure('LOGIN_FORM_UNQUALIFIED')
    # Only these five pre-approved nonsecure static values are challenge reads.
    chars=[]
    for i in range(17,22):
        ref=children[i]
        if get(ref,'AXRole')!='AXStaticText' or get(ref,'AXSubrole')=='AXSecureTextField':raise LoginFailure('CHALLENGE_UNAVAILABLE')
        value=get(ref,'AXValue')
        if type(value) is not str or not re.fullmatch('[0-9]',value):raise LoginFailure('CHALLENGE_UNAVAILABLE')
        chars.append(value)
    # Include every direct child's role/identity; no editable values in proof.
    proof=(key(root),key(sheet),tuple(nodes),(sx,sy,sw,sh),tuple(sorted(bounds.items())))
    return proof,tuple(children),''.join(chars)

def metadata():
    return {'status':'AX_TEXT_VERIFIED','character_count':5,'samples_equal':True,'digit_characters':True,'indices':list(range(17,22)),'value_exported':False}

def _dirsync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def _marker_read(path):
    from .login_retry import _read_record, _pairs
    try:
        raw,_=_read_record(path)
        data=json.loads(raw,object_pairs_hook=_pairs)
        if (type(data) is not dict or set(data)!={'schema','state','success_ns'} or data['schema']!='THS_LOGIN_ATTEMPT_V1'
                or data['state']!='SUCCESS' or type(data['success_ns']) is not int or not 0<data['success_ns']<=time.time_ns()):raise ValueError()
        return data
    except FileNotFoundError:return None
    except Exception:raise LoginFailure('LOGIN_ALREADY_CLAIMED') from None

def _persist(path,state,*,exclusive=False):
    data={'schema':'THS_LOGIN_ATTEMPT_V1','state':state,'success_ns':time.time_ns() if state=='SUCCESS' else 0}
    tmp=path if exclusive else path.with_suffix('.success.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        f.write(json.dumps(data)+'\n');f.flush();os.fsync(f.fileno())
    if not exclusive:os.replace(tmp,path)
    _dirsync(path.parent)

def attempt(owner,budget,*,allow_success_rearm=False):
    return _attempt(owner,budget,allow_success_rearm=allow_success_rearm)

def _attempt(owner,budget,*,allow_success_rearm=False,retry_marker=None,retry_guard=None,success_after_ns=None):
    result={'status':'LOGIN_FORM_UNQUALIFIED','press_attempted':False,'execution_enabled':False,'transaction_qualified':False}
    if not owner.config.get('allow_navigation',False):return {**result,'status':'NAVIGATION_DISABLED'}
    secret=None;claimed=False;claimed_record=None
    try:
        owner._owner_proof();budget.check()
        marker=retry_marker if retry_marker is not None else Path(owner._gui_owner._root)/'login-auto-attempt.json'
        if retry_guard is not None:retry_guard()
        previous=_marker_read(marker) # UNKNOWN fences before credential reads.
        if success_after_ns is not None and (not previous or previous['success_ns']<=success_after_ns):
            raise LoginFailure('LOGIN_FRESH_USED')
        if previous and (not allow_success_rearm or time.time_ns()-previous['success_ns']<SUCCESS_COOLDOWN_NS):raise LoginFailure('LOGIN_ALREADY_CLAIMED')
        app,session,identity=owner._connect();root=session.scope().root
        key=app.backend.key
        binding=(identity,key(app._root),key(root))
        # Explicit AX references do not require target foreground. Retain
        # foreground continuity during the attempt, without activating the app.
        front=owner._owned_effect(owner._runtime.front)
        proof,children,challenge=discover(owner,app,root)
        def validate():
            owner._owner_proof();budget.check()
            if retry_guard is not None:retry_guard()
            if claimed_record is not None and _read_record(marker)!=claimed_record:
                raise LoginFailure('LOGIN_ALREADY_CLAIMED')
            if owner._original is not None or owner._attempt is not None:raise LoginFailure('NATIVE_SESSION_UNCERTAIN')
            if (owner._owned_effect(owner._runtime.front)!=front or owner._owned_effect(owner._runtime.app_identity)!=identity
                    or key(app.backend.root(app.pid))!=binding[1] or key(session.scope().root)!=binding[2]):raise LoginFailure('LOGIN_TARGET_CHANGED')
            fresh,refs,text=discover(owner,app,root)
            if fresh!=proof or text!=challenge or tuple(app.backend.key(c) for c in refs)!=tuple(app.backend.key(c) for c in children):raise LoginFailure('LOGIN_TARGET_CHANGED')
        validate() # Second full sample, before opening credential store.
        result['ax_challenge']=metadata()
        secret=read_password(owner.config.get('login_secret_file'));validate()
        if previous:
            # Serialized GUI owner and process-exclusive lease guard this replacement.
            # Keep the marker present across renewal, including interrupted writes.
            if _marker_read(marker)!=previous:raise LoginFailure('LOGIN_ALREADY_CLAIMED')
        try:_persist(marker,'UNKNOWN',exclusive=not previous)
        except FileExistsError:raise LoginFailure('LOGIN_ALREADY_CLAIMED') from None
        claimed=True
        from .login_retry import _read_record, _pairs
        claimed_record=_read_record(marker)
        row=json.loads(claimed_record[0],object_pairs_hook=_pairs)
        if row!={'schema':'THS_LOGIN_ATTEMPT_V1','state':'UNKNOWN','success_ns':0} or type(row['success_ns']) is not int:
            raise LoginFailure('LOGIN_ALREADY_CLAIMED')
        def write(index,value):
            validate()
            def dispatch():
                owner._owner_proof();budget.check();rb.mark_rpc()
                return owner._runtime.backend.set(children[index],'AXValue',value)
            owner._owned_effect(dispatch)
        write(11,secret);secret=None
        write(15,challenge)
        validate();result['press_attempted']=True
        def submit():
            owner._owner_proof();budget.check();rb.mark_rpc()
            return owner._runtime.backend.perform(children[22],'AXPress')
        owner._owned_effect(submit)
        # Bounded read-only settlement, never another input or another budget.
        from .._ax.errors import ElementNotFound
        for _ in range(24):
            budget.check()
            try:
                after=owner._login_entry('login-status',budget,binding=binding)
                if after['already_logged_in']:
                    if retry_guard is not None:retry_guard()
                    if _read_record(marker)!=claimed_record:raise LoginFailure('LOGIN_ALREADY_CLAIMED')
                    owner._owner_proof();budget.check()
                    _persist(marker,'SUCCESS')
                    return {**result,'status':'LOGIN_VERIFIED'}
                if after['status']=='LOGIN_COMMUNICATION_ERROR':
                    return {**result,**{k:after[k] for k in ('status','authenticated','authentication_state','observed_at','provenance')}}
            except ElementNotFound:pass
            time.sleep(budget.remaining_seconds(.05))
        return {**result,'status':'LOGIN_UNKNOWN'}
    except LoginFailure as exc:return {**result,'status':'LOGIN_UNKNOWN' if claimed else exc.code}
    except Exception:
        if claimed:return {**result,'status':'LOGIN_UNKNOWN'}
        raise
    finally:secret=None

# Public status vocabulary is shared with the settings donor; no raw native text.
ENSURE_STATUS=frozenset(('LOGIN_COMMUNICATION_ERROR','LOGIN_RESULT_UNAVAILABLE','LOGIN_RESULT_UNRECOGNIZED','ALREADY_LOGGED_IN','LOGIN_VERIFIED','LOGIN_UNKNOWN',
    'CREDENTIAL_REQUIRED','LOGIN_ALREADY_CLAIMED','LOGIN_ENTRY_BLOCKED',
    'LOGIN_CONTROL_NOT_UNIQUE','LOGIN_CONTROL_UNSAFE','LOGIN_TARGET_CHANGED',
    'LOGIN_TARGET_NOT_FRONT','LOGIN_FORM_ALREADY_PRESENT','NAVIGATION_DISABLED',
    'CHALLENGE_UNAVAILABLE','LOGIN_ENTRY_INSPECTED','TRADE_PAGE_VERIFIED',
    'ALREADY_TRADE_PAGE','TRADE_LOGIN_ENTRY_VERIFIED','LOGIN_DIALOG_OPENED',
    'LOGIN_FORM_UNQUALIFIED','NATIVE_SESSION_UNCERTAIN','SWITCH_ALREADY_CLAIMED',
    'SWITCH_BLOCKED','TRADE_CONTROL_NOT_UNIQUE','TRADE_CONTROL_UNSAFE',
    'LOGIN_RETRY_USED','LOGIN_RETRY_BLOCKED','LOGIN_RETRY_COOLDOWN','LOGIN_RETRY_FORM_REQUIRED',
    'LOGIN_FRESH_USED','LOGIN_FRESH_BLOCKED','LOGIN_FRESH_AVAILABLE','LOGIN_RECOVERY_COOLDOWN'))

def ensure(owner,budget):
    """One explicit request, original owner/12s budget across every phase."""
    rb.require(budget);owner._owner_proof()
    steps=[]
    def record(result):
        status=result.get('status')
        status=status if status in ENSURE_STATUS else 'LOGIN_UNKNOWN'
        if len(steps)<8:steps.append(status)
        return status
    def finish(result):
        status=record(result)
        safe={k:result[k] for k in ('ax_challenge','already_logged_in','authenticated','authentication_state','observed_at','provenance') if k in result}
        return {'status':status,'steps':list(steps),'press_attempted':bool(result.get('press_attempted')),
                'execution_enabled':False,'transaction_qualified':False,**safe}
    current=owner._login_entry('login-status',budget)
    if current['already_logged_in']:return finish(current)
    if current['status']=='LOGIN_COMMUNICATION_ERROR':return finish(current)
    record(current)
    from .login_retry import FRESH, ORIGINAL, _spent_legacy, historical, legacy_artifacts, pending_writes
    directory=Path(owner._gui_owner._root)
    marker=directory/ORIGINAL
    fresh_marker=None
    try:cohort=legacy_artifacts(directory)
    except (OSError,LoginFailure):return finish({'status':'LOGIN_FRESH_BLOCKED'})
    if pending_writes(directory):return finish({'status':'LOGIN_FRESH_BLOCKED'})
    if cohort:
        if not os.path.lexists(directory/FRESH):
            try:_spent_legacy(marker.parent)
            except LoginFailure as exc:
                return finish({'status':'LOGIN_RETRY_COOLDOWN' if exc.code=='LOGIN_RETRY_COOLDOWN' else 'LOGIN_RETRY_USED'})
            except (OSError,ValueError):return finish({'status':'LOGIN_RETRY_USED'})
            return finish({'status':'LOGIN_FRESH_AVAILABLE'})
        try:
            fresh_marker=marker.parent/FRESH
            previous=_marker_read(fresh_marker)
            if not previous:return finish({'status':'LOGIN_FRESH_USED'})
            legacy=_spent_legacy(marker.parent)
            if previous['success_ns']<=legacy[-1][2]:return finish({'status':'LOGIN_FRESH_USED'})
        except (OSError, ValueError, LoginFailure):return finish({'status':'LOGIN_FRESH_USED'})
    else:
        if os.path.lexists(directory/FRESH):return finish({'status':'LOGIN_FRESH_BLOCKED'})
        try:previous=_marker_read(marker)
        except LoginFailure:
            try:historical(marker)
            except (OSError,ValueError,LoginFailure):return finish({'status':'LOGIN_FRESH_BLOCKED'})
            return finish({'status':'LOGIN_ALREADY_CLAIMED'})
    if previous and time.time_ns()-previous['success_ns']<SUCCESS_COOLDOWN_NS:return finish({'status':'LOGIN_RECOVERY_COOLDOWN'})
    if owner._original is not None or owner._attempt is not None:return finish({'status':'NATIVE_SESSION_UNCERTAIN'})
    from .disconnect_warning import before_open
    warning=before_open(owner,budget,explicit_navigation=fresh_marker is not None)
    if warning is not None:return finish(warning)
    # Fresh login control proof only after the warning has disappeared.
    current=owner._login_entry('login-status',budget)
    if current['already_logged_in']:return finish(current)
    if current['status']=='LOGIN_COMMUNICATION_ERROR':return finish(current)
    if current['status']=='LOGIN_RESULT_UNRECOGNIZED':return finish({'status':'LOGIN_ENTRY_BLOCKED'})
    form=any(f['signature'] and f['role']=='AXSheet' for f in current['auth_forms'])
    fresh_expired=form or current['login_control_count']==1
    if not form and not current['login_control_count']:
        nav=owner.trade_page('switch',budget=budget)
        if nav['status'] not in ('TRADE_PAGE_VERIFIED','ALREADY_TRADE_PAGE','TRADE_LOGIN_ENTRY_VERIFIED'):return finish(nav)
        record(nav)
        current=owner._login_entry('login-status',budget)
        if current['already_logged_in']:return finish(current)
        if current['status']=='LOGIN_COMMUNICATION_ERROR':return finish(current)
        if current['status']=='LOGIN_RESULT_UNRECOGNIZED':return finish({'status':'LOGIN_ENTRY_BLOCKED'})
        form=any(f['signature'] and f['role']=='AXSheet' for f in current['auth_forms'])
        fresh_expired=form or current['login_control_count']==1
    if not form:
        opened=owner._login_entry('login-open',budget,narrow=True)
        if opened['status'] not in ('LOGIN_DIALOG_OPENED','LOGIN_FORM_ALREADY_PRESENT'):return finish(opened)
        record(opened)
    if not fresh_expired:return finish({'status':'LOGIN_FORM_UNQUALIFIED'})
    if fresh_marker is not None:
        def validate_legacy():
            try:
                if _spent_legacy(marker.parent)==legacy:return
            except (OSError,ValueError,LoginFailure):pass
            raise LoginFailure('LOGIN_FRESH_BLOCKED')
        return finish(_attempt(owner,budget,allow_success_rearm=True,retry_marker=fresh_marker,
                               retry_guard=validate_legacy,success_after_ns=legacy[-1][2]))
    def validate_ordinary():
        if legacy_artifacts(directory) or os.path.lexists(directory/FRESH) or pending_writes(directory):
            raise LoginFailure('LOGIN_FRESH_BLOCKED')
    return finish(_attempt(owner,budget,allow_success_rearm=True,retry_guard=validate_ordinary))
