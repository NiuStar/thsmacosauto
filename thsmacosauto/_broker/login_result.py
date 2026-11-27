"""Passive fixed-label outcome proof; no editable values or input actions."""
from collections import deque
from datetime import datetime, timezone
from . import readonly_budget as rb

class Unrecognized(Exception):pass

# Shared structural traversal for the modal veto. Never read list contents as
# text or descend into editable fields, data tables or menus.
MODAL_CONTAINERS=frozenset(('AXWindow','AXSheet','AXDialog','AXGroup','AXSplitGroup',
                           'AXScrollArea','AXTabGroup','AXToolbar','AXPopover','AXList'))

def is_modal(get,ref,role,subrole):
    # Match the pinned framework's window guard even without AXModal.
    return (role in ('AXSheet','AXDialog','AXPopover')
            or (role=='AXWindow' and subrole in ('AXDialog','AXSystemDialog'))
            or get(ref,'AXModal') is True)

def modal_census(owner,app,root):
    """Bounded, structure-only census across every application window."""
    get=owner._get;key=app.backend.key
    windows=get(app._root,'AXWindows')
    if not isinstance(windows,(list,tuple)) or not 0<len(windows)<=12:raise Unrecognized()
    window_keys=tuple(map(key,windows))
    if len(set(window_keys))!=len(windows) or key(root) not in window_keys:raise Unrecognized()
    queue=deque((w,0,None,k) for w,k in zip(windows,window_keys));seen={};modals=[]
    while queue:
        rb.check();ref,depth,parent,window=queue.popleft();k=key(ref)
        if k in seen:
            if seen[k]!=(window,parent):raise Unrecognized()
            continue
        seen[k]=(window,parent)
        if len(seen)>min(app.max_nodes,600) or depth>min(app.max_depth,20):raise Unrecognized()
        role=get(ref,'AXRole');sub=get(ref,'AXSubrole')
        if (not depth and role!='AXWindow') or (depth and role=='AXWindow'):raise Unrecognized()
        if sub=='AXSecureTextField' or role not in MODAL_CONTAINERS:continue
        children=get(ref,'AXChildren');extra=get(ref,'AXSheets') if role in ('AXWindow','AXSheet') else None
        if children is None:children=()
        if extra is None:extra=()
        if not isinstance(children,(list,tuple)) or not isinstance(extra,(list,tuple)) or len(children)+len(extra)>600 or len({key(c) for c in children})!=len(children):raise Unrecognized()
        if is_modal(get,ref,role,sub):
            modals.append((ref,k,role,parent,window,children))
        queue.extend((c,depth+1,k,window) for c in (*children,*extra))
        if len(queue)>1200:raise Unrecognized()
    return window_keys,modals

def scan(owner,app,root):
    get=owner._get;key=app.backend.key
    windows,nodes=modal_census(owner,app,root)
    modals=[];found=[];auth=[]
    for ref,k,role,parent,window,children in nodes:
        modals.append(k)
        if role=='AXSheet' and get(ref,'AXIdentifier')=='_NS:259':
            p=get(ref,'AXParent')
            if window!=key(root) or parent!=window or p is None or key(p)!=window:raise Unrecognized()
            auth.append(k)
        if role=='AXSheet' and get(ref,'AXIdentifier')=='_NS:87':
            p=get(ref,'AXParent')
            if p is None or key(p)!=parent or get(p,'AXRole')!='AXSheet' or get(p,'AXIdentifier')!='_NS:259':raise Unrecognized()
            pp=get(p,'AXParent')
            if pp is None or key(pp)!=key(root) or get(root,'AXRole')!='AXWindow':raise Unrecognized()
            if len(children)!=4 or [get(c,'AXRole') for c in children]!=['AXImage','AXStaticText','AXStaticText','AXButton'] or [get(c,'AXIdentifier') for c in children]!=['_NS:35','_NS:74','_NS:58','action-button-1']:raise Unrecognized()
            if any(get(c,'AXSubrole')=='AXSecureTextField' or get(c,'AXParent') is None or key(get(c,'AXParent'))!=k for c in children):raise Unrecognized()
            if get(children[1],'AXValue')!='警告' or get(children[2],'AXValue')!='通讯异常，请稍候重试!' or get(children[3],'AXTitle')!='确认' or get(children[3],'AXEnabled') is not True or 'AXPress' not in app.backend.actions(children[3]):raise Unrecognized()
            found.append((k,key(p),tuple(key(c) for c in children)))

    if not found:
        # An ordinary open auth sheet has no outcome; another modal is unknown.
        if modals and (len(modals)!=1 or modals!=auth):raise Unrecognized()
        return None,tuple(modals),windows
    if len(found)!=1 or set(modals)!={found[0][0],found[0][1]}:raise Unrecognized()
    return found[0],tuple(modals),windows

def observe(owner,budget,app,session,identity,root,logged,*,assert_binding=lambda:None):
    owner._owner_proof();budget.check()
    key=app.backend.key
    def continuity():
        owner._owner_proof();budget.check()
        if owner._owned_effect(owner._runtime.app_identity)!=identity or key(session.scope().root)!=key(root):raise Unrecognized()
        assert_binding()
    try:
        continuity();first=scan(owner,app,root)
        first_logged=logged() if callable(logged) and not first[0] and not first[1] else False
        continuity();second=scan(owner,app,root)
        second_logged=logged() if callable(logged) and not second[0] and not second[1] else False
        continuity();final=scan(owner,app,root);continuity()
        if first!=second or second!=final or first_logged!=second_logged:raise Unrecognized()
        status='LOGIN_COMMUNICATION_ERROR' if first[0] else ('ALREADY_LOGGED_IN' if first_logged and second_logged and not first[1] else 'LOGIN_RESULT_UNAVAILABLE')
    except Unrecognized:status='LOGIN_RESULT_UNRECOGNIZED'
    return {'status':status,'authenticated':status=='ALREADY_LOGGED_IN','authentication_state':'VERIFIED' if status=='ALREADY_LOGGED_IN' else 'UNVERIFIED',
            'observed_at':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),'provenance':'GUI_CURRENT'}
