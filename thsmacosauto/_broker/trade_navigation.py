"""Trade-page inspection and explicit navigation; never submits credentials."""
from .native_errors import _block


def trade_page(owner, action, budget):
    if action == 'switch' and getattr(owner,'_trade_page_claimed',False):
        return {'status':'SWITCH_ALREADY_CLAIMED','press_attempted':False,'execution_enabled':False}
    owner._owner_proof()
    app, session, identity = owner._connect()
    root = session.scope().root
    switch_front=owner._owned_effect(owner._runtime.front) if action=='switch' else None
    trade_labels = frozenset(('交易','委托交易','股票交易','交易委托'))
    labels = trade_labels | frozenset(('自选','行情','资讯','发现','首页','问财','资金','银证','持仓','买入','卖出','委托','成交','天府证券'))
    roles = frozenset(('AXButton','AXRadioButton','AXCheckBox','AXTab'))
    path_roles=roles | frozenset(('AXWindow','AXGroup','AXSplitGroup','AXScrollArea','AXTabGroup','AXToolbar','AXTable','AXList','AXOutline','AXStaticText','AXImage'))
    def safe_identifier(ref):
        import re
        ident=owner._get(ref,'AXIdentifier')
        return ident if type(ident) is str and re.fullmatch(r'_NS:[0-9]{1,5}',ident) else None
    # References stay private to this call. All outbound labels are literals.
    def discover():
        from collections import deque
        queue = deque([(root,0,())])
        seen=set();controls=[];candidates=[];counts={label:0 for label in labels};unsafe=False;hints=[];sidebar_nodes=[]
        pruned=[]
        while queue:
            ref,depth,path=queue.popleft()
            key=app.backend.key(ref)
            if key in seen:continue
            seen.add(key)
            if len(seen)>app.max_nodes:_block('AX_TRAVERSAL_LIMIT')
            role=owner._get(ref,'AXRole');subrole=owner._get(ref,'AXSubrole')
            path=path+(role if role in path_roles else 'AXOther',)
            if subrole=='AXSecureTextField' or role in ('AXSecureTextField','AXTextField','AXTextArea','AXMenu','AXMenuBar','AXSheet','AXDialog'):
                if role in ('AXSheet','AXDialog'):unsafe=True
                continue
            if action=='inspect' and path[:2]==('AXWindow','AXList'):
                words=[owner._get(ref,a) for a in ('AXTitle','AXDescription','AXHelp')]
                if role in ('AXStaticText','AXButton'):words.append(owner._get(ref,'AXValue'))
                sidebar_nodes.append({'role':role if role in path_roles else 'AXOther',
                    'identifier':safe_identifier(ref),'depth':depth,
                    'labels':sorted({w for w in words if type(w) is str and w in labels}),
                    'press':('AXPress' in (app.backend.actions(ref) or [])) if role in roles else False})
            if role=='AXStaticText':
                value=owner._get(ref,'AXValue')
                if isinstance(value,str):
                    if value in counts:counts[value]+=1
                    if any(marker in value for marker in owner.profile['forbidden_text']):unsafe=True
            if role in roles:
                attrs=('AXTitle','AXDescription','AXHelp')
                if role=='AXButton':attrs+=('AXValue',)
                values=[owner._get(ref,attr) for attr in attrs]
                # An explicit title relation is owned by this control even
                # when the title element is not an AXChildren member.
                title_ref=owner._get(ref,'AXTitleUIElement')
                if title_ref is not None:
                    title_role=owner._get(title_ref,'AXRole')
                    title_subrole=owner._get(title_ref,'AXSubrole')
                    if title_subrole!='AXSecureTextField' and title_role=='AXStaticText':
                        values.extend(owner._get(title_ref,a) for a in ('AXValue','AXTitle'))
                # Only immediate, nonsecure label/image children belonging
                # to this action control may contribute navigation wording.
                children=owner._get(ref,'AXChildren') or []
                if not isinstance(children,(list,tuple)) or len(children)>app.max_nodes:
                    _block('AX_TRAVERSAL_LIMIT')
                if depth>=app.max_depth and children:_block('AX_TRAVERSAL_LIMIT')
                for child in children:
                    child_role=owner._get(child,'AXRole')
                    child_subrole=owner._get(child,'AXSubrole')
                    if child_subrole=='AXSecureTextField' or child_role not in ('AXStaticText','AXImage'):
                        continue
                    parent=owner._get(child,'AXParent')
                    if parent is None or app.backend.key(parent)!=key:continue
                    child_attrs=('AXValue','AXTitle') if child_role=='AXStaticText' else ('AXDescription','AXTitle','AXHelp')
                    values.extend(owner._get(child,attr) for attr in child_attrs)
                known=sorted({v for v in values if type(v) is str and v in labels})
                # Export only fixed labels, never arbitrary native wording.
                hints.extend({'role':role,'attribute':attr,'text':value}
                    for attr,value in zip(attrs,values)
                    if type(value) is str and value in trade_labels)
                if any(any(marker in v for marker in owner.profile['forbidden_text']) for v in values if isinstance(v,str)):unsafe=True
                # Conflicting descriptions never grant a trade navigation.
                enabled=owner._get(ref,'AXEnabled') is True
                actions=app.backend.actions(ref)
                press=isinstance(actions,(list,tuple)) and 'AXPress' in actions
                selected=owner._get(ref,'AXValue') if role in ('AXRadioButton','AXCheckBox','AXTab') else None
                selected=bool(selected) if type(selected) in (bool,int) and selected in (0,1) else None
                # Native identifiers may contain account values. Only the
                # known NS numeric identifier grammar crosses this boundary.
                ident=safe_identifier(ref)
                facts={'role':role,'identifier':ident,'labels':known,'enabled':enabled,
                       'actions':['AXPress'] if press else [],'selected':selected}
                controls.append(facts)
                if len(known)==1 and known[0] in trade_labels:
                    candidates.append((ref,facts))
            # The main window's direct AXList is the native sidebar.
            # Never enter financial tables or nested lists/outlines.
            direct_sidebar = role=='AXList' and path[:2]==('AXWindow','AXList') and depth<=4
            if role in ('AXTable','AXList','AXOutline') and not direct_sidebar:
                if action=='inspect':
                    pruned.append({'role':role,'identifier':safe_identifier(ref),'window_path':list(path)})
                continue
            children=owner._get(ref,'AXChildren') or []
            if ref==root:
                children=list(children)
                for relation in ('AXToolbar','AXContents','AXChildrenInNavigationOrder'):
                    extra=owner._get(ref,relation)
                    if isinstance(extra,(list,tuple)):children.extend(extra)
                    elif extra is not None and not isinstance(extra,(str,bool,int,float)):children.append(extra)
            if not isinstance(children,(list,tuple)):_block('AX_TRAVERSAL_LIMIT')
            if depth>=app.max_depth and children:_block('AX_TRAVERSAL_LIMIT')
            queue.extend((child,depth+1,path) for child in children)
        # User-confirmed screenshot profile: main-window direct children
        # 1..9 are nine 56pt sidebar controls; child 6 is trade. Re-prove
        # the entire relative layout each discovery; never click pixels.
        if not candidates:
            import math
            children=owner._get(root,'AXChildren') or []
            pos=owner._get(root,'AXPosition')
            shape=len(children)>=10 and isinstance(pos,dict)
            if shape:
                for i in range(1,10):
                    item=children[i]
                    sub=owner._get(item,'AXSubrole');role=owner._get(item,'AXRole')
                    if sub=='AXSecureTextField' or role!='AXButton':shape=False;break
                    p=owner._get(item,'AXPosition');s=owner._get(item,'AXSize')
                    if not isinstance(p,dict) or not isinstance(s,dict):shape=False;break
                    observed=(p.get('x'),p.get('y'),s.get('width'),s.get('height'),pos.get('x'),pos.get('y'))
                    if any(type(v) not in (int,float) or not math.isfinite(v) for v in observed):shape=False;break
                    if not (abs(p['x']-pos['x']+1)<=2 and abs(p['y']-pos['y']-(91+(i-1)*54))<=2 and abs(s['width']-56)<=2 and abs(s['height']-56)<=2):shape=False;break
            if shape:
                target=children[6]
                effects=app.backend.actions(target)
                facts={'role':'AXButton','identifier':safe_identifier(target),'labels':['交易'],
                    'enabled':owner._get(target,'AXEnabled') is True,
                    'actions':['AXPress'] if 'AXPress' in effects else [],'selected':None,
                    'locator':'USER_CONFIRMED_SIDEBAR_CHILD_6','child_index':6}
                candidates.append((target,facts));controls.append(facts)
        return controls,candidates,counts,unsafe,hints,pruned,sidebar_nodes
    controls,candidates,counts,unsafe,hints,pruned,sidebar_nodes=discover()
    panel=None
    if counts['天府证券']==1 and not unsafe:
        try:
            detected=session.detect()['page']
            if detected in ('持仓','委托','成交'):panel=detected
        except Exception as exc:
            from .._ax.errors import ElementNotFound, AmbiguousMatch
            if not isinstance(exc,(ElementNotFound,AmbiguousMatch)):raise
    result={'status':'TRADE_PAGE_INSPECTED','press_attempted':False,'execution_enabled':False,
            'trade_page_verified':panel is not None,'panel':panel,'forbidden_or_modal':unsafe,
            'known_labels':counts,'controls':controls,'trade_control_count':len(candidates),'navigation_hints':hints}
    if action=='inspect':
        windows=owner._get(app._root,'AXWindows') or []
        if not isinstance(windows,(list,tuple)) or len(windows)>12:_block('AX_TRAVERSAL_LIMIT')
        census=[]
        for win in windows:
            if owner._get(win,'AXSubrole')=='AXSecureTextField':continue
            facts={'is_current':app.backend.key(win)==app.backend.key(root),'identifier':safe_identifier(win),'roles':{},'navigation':[]}
            facts['position']=owner._get(win,'AXPosition');facts['size']=owner._get(win,'AXSize');facts['indexed_nodes']=[]
            pending=[(win,0,[])];visited=set()
            while pending:
                item,level,index_path=pending.pop(0);key=app.backend.key(item)
                if key in visited:continue
                visited.add(key)
                if len(visited)>app.max_nodes:_block('AX_TRAVERSAL_LIMIT')
                sub=owner._get(item,'AXSubrole');role=owner._get(item,'AXRole')
                if sub=='AXSecureTextField' or role in ('AXTextField','AXTextArea','AXSecureTextField','AXMenu','AXMenuBar'):continue
                safe_role=role if role in path_roles else 'AXOther'
                facts['roles'][safe_role]=facts['roles'].get(safe_role,0)+1
                if role in roles or level<=2:
                    facts['indexed_nodes'].append({'path':index_path,'role':safe_role,'identifier':safe_identifier(item),'position':owner._get(item,'AXPosition'),'size':owner._get(item,'AXSize')})
                if role in roles or role=='AXStaticText':
                    words=[owner._get(item,a) for a in ('AXTitle','AXDescription','AXHelp')]
                    if role in ('AXButton','AXStaticText'):words.append(owner._get(item,'AXValue'))
                    matched=sorted({w for w in words if type(w) is str and w in labels})
                    if matched:facts['navigation'].append({'role':safe_role,'labels':matched,'identifier':safe_identifier(item)})
                if role in ('AXTable','AXList','AXOutline','AXSheet','AXDialog'):continue
                children=owner._get(item,'AXChildren') or []
                if level>=app.max_depth and children:_block('AX_TRAVERSAL_LIMIT')
                pending.extend((child,level+1,index_path+[i]) for i,child in enumerate(children))
            census.append(facts)
        return {**result,'pruned_containers':pruned,'sidebar_nodes':sidebar_nodes,'windows_census':census}

    if panel is not None:return {**result,'status':'ALREADY_TRADE_PAGE'}
    if unsafe or counts['天府证券']>0:return {**result,'status':'SWITCH_BLOCKED'}
    if not owner.config.get('allow_navigation',False):return {**result,'status':'NAVIGATION_DISABLED'}
    if len(candidates)!=1:return {**result,'status':'TRADE_CONTROL_NOT_UNIQUE'}
    ref,facts=candidates[0]
    if not facts['enabled'] or 'AXPress' not in facts['actions'] or facts['selected'] is True:
        return {**result,'status':'TRADE_CONTROL_UNSAFE'}
    # Prove identity and fresh window membership again before claiming input.
    if owner._owned_effect(owner._runtime.app_identity)!=identity:_block('NATIVE_CONTINUITY_CHANGED')
    if app.backend.key(session.scope().root)!=app.backend.key(root):_block('NATIVE_CONTINUITY_CHANGED')
    _,fresh,fresh_counts,fresh_unsafe,_,_,_=discover()
    if fresh_unsafe or fresh_counts!=counts or len(fresh)!=1 or app.backend.key(fresh[0][0])!=app.backend.key(ref) or fresh[0][1]!=facts:
        return {**result,'status':'SWITCH_BLOCKED'}
    if owner._owned_effect(owner._runtime.app_identity)!=identity:_block('NATIVE_CONTINUITY_CHANGED')
    if app.backend.key(session.scope().root)!=app.backend.key(root):_block('NATIVE_CONTINUITY_CHANGED')
    if not owner._navigation_modal_clear(app,session,identity,root,switch_front):return {**result,'status':'SWITCH_BLOCKED'}
    owner._owner_proof()
    budget.check()
    # Claimed BEFORE dispatch: even an uncertain native failure cannot replay.
    owner._trade_page_claimed=True
    result.update(status='SWITCH_UNCONFIRMED',press_attempted=True,trade_page_verified=False,panel=None)
    try:
        def dispatch():
            return owner._press_operator_control(ref)
        owner._owned_effect(dispatch)
        app2,session2,identity2=owner._connect()
        if identity2!=identity or app2.backend.key(session2.scope().root)!=app.backend.key(root):
            return result
        auth=owner._login_entry('login-status',budget,binding=(identity,app.backend.key(app._root),app.backend.key(root)))
        if auth['status'] in ('LOGIN_RESULT_UNRECOGNIZED','LOGIN_COMMUNICATION_ERROR'):return result
        if (not auth['already_logged_in'] and auth['login_control_count']==1 and not any(f['role'] in ('AXSheet','AXDialog') for f in auth['auth_forms'])) or any(f['signature'] and f['role']=='AXSheet' for f in auth['auth_forms']):
            owner._trade_page_claimed=False
            result.update(status='TRADE_LOGIN_ENTRY_VERIFIED')
            return result
        owner._identity(app2,session2)
        panel=session2.detect()['page']
        if panel in ('持仓','委托','成交'):
            result.update(status='TRADE_PAGE_VERIFIED',trade_page_verified=True,panel=panel)
            owner._trade_page_claimed=False
    except Exception:
        # Fixed uncertain result, never native exception text; slot remains
        # owned until the original Engine drains/finalizes this budget.
        return result
    return result
