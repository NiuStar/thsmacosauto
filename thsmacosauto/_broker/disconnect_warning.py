"""Exact warning-only AXPress. No focus, coordinates, field values or retries."""
from pathlib import Path
from . import readonly_budget as rb
from .login_result import modal_census, Unrecognized

# Only the screenshot reading confirmed so far; extend only with native evidence.
MESSAGES=frozenset(('用户连接已被主站断开，请重新登录！',))
class Refused(Exception):pass

def scan(owner,app,root):
    get=owner._get;key=app.backend.key
    try:windows,modals=modal_census(owner,app,root)
    except Unrecognized:raise Refused() from None
    if not modals:return None
    if len(modals)!=1:raise Refused()
    ref,k,role,parent,window,children=modals[0]
    if role!='AXSheet' or parent!=window or window!=key(root):raise Refused()
    actual=get(ref,'AXParent')
    if actual is None or key(actual)!=parent:raise Refused()
    ident=get(ref,'AXIdentifier')
    if ident=='_NS:259':return None  # No warning action; the auth form has its own proof.
    if ident!='_NS:87' or len(children)!=4:raise Refused()
    roles=tuple(get(c,'AXRole') for c in children)
    if roles!=('AXImage','AXStaticText','AXStaticText','AXButton') or [get(c,'AXIdentifier') for c in children]!=['_NS:35','_NS:74','_NS:58','action-button-1']:raise Refused()
    for c in children:
        p=get(c,'AXParent')
        if p is None or key(p)!=k or get(c,'AXSubrole')=='AXSecureTextField':raise Refused()
    _,heading,message,button=children
    if get(heading,'AXValue')!='警告' or get(message,'AXValue') not in MESSAGES:raise Refused()
    if get(button,'AXTitle')!='确认' or get(button,'AXEnabled') is not True or 'AXPress' not in app.backend.actions(button):raise Refused()
    return (k,tuple(map(key,children)),roles,parent,windows),button

def inspect(owner,app,root):
    try:found=scan(owner,app,root)
    except Refused:return {'status':'DISCONNECT_WARNING_BLOCKED'}
    return {'status':'DISCONNECT_WARNING_PRESENT' if found else 'DISCONNECT_WARNING_ABSENT'}

def before_open(owner,budget,*,explicit_navigation=False):
    """Default auth fence; explicit navigation skips only auth-marker reads."""
    owner._owner_proof();budget.check()
    app,session,identity=owner._connect();root=session.scope().root
    try:found=scan(owner,app,root)
    except Refused:return {'status':'LOGIN_ENTRY_BLOCKED','press_attempted':False}
    marker=Path(owner._gui_owner._root)/'disconnect-warning-attempt.json'
    if marker.exists():return {'status':'LOGIN_ALREADY_CLAIMED','press_attempted':False}
    if found is None:return None
    if not owner.config.get('allow_navigation',False):return {'status':'NAVIGATION_DISABLED','press_attempted':False}
    if owner._original is not None or owner._attempt is not None:return {'status':'NATIVE_SESSION_UNCERTAIN','press_attempted':False}
    from .login_auto import _marker_read,_persist,LoginFailure,_dirsync
    if not explicit_navigation:
        try:_marker_read(Path(owner._gui_owner._root)/'login-auto-attempt.json')
        except LoginFailure:return {'status':'LOGIN_ALREADY_CLAIMED','press_attempted':False}
    front=owner._owned_effect(owner._runtime.front);key=app.backend.key;app_key=key(app._root)
    proof,_=found
    def validate():
        owner._owner_proof();budget.check()
        if owner._original is not None or owner._attempt is not None or owner._owned_effect(owner._runtime.front)!=front or owner._owned_effect(owner._runtime.app_identity)!=identity or key(app.backend.root(app.pid))!=app_key or key(session.scope().root)!=key(root):raise Refused()
        fresh=scan(owner,app,root)
        if fresh is None or fresh[0]!=proof:raise Refused()
        return fresh[1]
    try:button=validate()
    except Refused:return {'status':'LOGIN_TARGET_CHANGED','press_attempted':False}
    try:_persist(marker,'UNKNOWN',exclusive=True)
    except FileExistsError:return {'status':'LOGIN_ALREADY_CLAIMED','press_attempted':False}
    dispatched=False
    try:
        # Final re-resolution occurs under the same owner immediately before AX.
        button=validate()
        def dispatch():
            nonlocal dispatched
            owner._owner_proof();budget.check();dispatched=True;rb.mark_rpc()
            return owner._runtime.backend.perform(button,'AXPress')
        owner._owned_effect(dispatch)
        for _ in range(8):
            owner._owner_proof();budget.check()
            if owner._owned_effect(owner._runtime.front)!=front or owner._owned_effect(owner._runtime.app_identity)!=identity or key(app.backend.root(app.pid))!=app_key or key(session.scope().root)!=key(root):raise Refused()
            if scan(owner,app,root) is None:
                # A new exact, unique immediate-login control is mandatory;
                # never fall through to page switching or the legacy paired fallback.
                state=owner._login_entry('login-status',budget,narrow=True,binding=(identity,app_key,key(root)))
                controls=state.get('login_controls',[])
                if state.get('status')=='LOGIN_RESULT_UNRECOGNIZED':raise Refused()
                if state.get('login_control_count')!=1 or len(controls)!=1 or controls[0].get('locator')!='EXACT_NATIVE_LABEL' or controls[0].get('in_modal') or not controls[0].get('enabled') or controls[0].get('actions')!=['AXPress']:
                    return {'status':'LOGIN_ENTRY_BLOCKED','press_attempted':True}
                marker.unlink();_dirsync(marker.parent)
                return None
            import time
            time.sleep(budget.remaining_seconds(.025))
    except Refused:
        if not dispatched:
            marker.unlink();_dirsync(marker.parent)
            return {'status':'LOGIN_TARGET_CHANGED','press_attempted':False}
    except Exception:
        if not dispatched:
            marker.unlink();_dirsync(marker.parent)
        raise
    return {'status':'LOGIN_UNKNOWN','press_attempted':dispatched,'execution_enabled':False,'transaction_qualified':False}
