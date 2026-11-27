"""Passive login/form observations; no credentials or native input."""
from .native_errors import _block


def _probe(owner, *, opening=False, narrow=False, binding=None):
    import math
    import re
    from collections import deque
    owner._owner_proof()
    app,session,identity=owner._connect()
    root=session.scope().root
    def assert_binding():
        if binding is None:return
        key=app.backend.key
        if ((identity,key(app._root),key(root))!=binding
                or key(app.backend.root(app.pid))!=binding[1]):
            from .login_auto import LoginFailure
            raise LoginFailure('LOGIN_TARGET_CHANGED')
    assert_binding()
    entry_front=owner._owned_effect(owner._runtime.front) if opening else None
    fixed=frozenset(('立即登录','免费开户','登录','自动登录','记住密码','验证码','账号','密码','交易密码','资金账号','取消','确定','天府证券','持仓','委托','成交','交易'))
    containers=frozenset(('AXWindow','AXSheet','AXDialog','AXGroup','AXSplitGroup','AXScrollArea','AXTabGroup','AXToolbar','AXList'))
    def geometry(ref):
        p=owner._get(ref,'AXPosition');s=owner._get(ref,'AXSize')
        if not isinstance(p,dict) or not isinstance(s,dict):return None
        values=[p.get('x'),p.get('y'),s.get('width'),s.get('height')]
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):return None
        return dict(zip(('x','y','width','height'),values))
    def identifier(ref):
        value=owner._get(ref,'AXIdentifier')
        return value if type(value) is str and re.fullmatch(r'_NS:[0-9]{1,5}',value) else None
    from .login_result import is_modal
    def sample(*, after_press=False):
        windows=owner._get(app._root,'AXWindows') or []
        if not isinstance(windows,(list,tuple)) or len(windows)>12:_block('AX_TRAVERSAL_LIMIT')
        window_keys=[app.backend.key(w) for w in windows]
        root_key=app.backend.key(root)
        if len(set(window_keys))!=len(window_keys) or root_key not in window_keys:_block('NATIVE_CONTINUITY_CHANGED')
        root_index=window_keys.index(root_key)
        queue=deque((w,0,[i],None,False) for i,w in enumerate(windows))
        seen={};entries=[];forms=[];counts={label:0 for label in fixed};modal=False;unsafe=False
        while queue:
            ref,depth,path,form,in_modal=queue.popleft();key=app.backend.key(ref)
            if depth and key in window_keys and key!=window_keys[path[0]]:continue
            prior_window=seen.get(key)
            if prior_window is not None:
                if prior_window!=path[0]:unsafe=True
                continue
            seen[key]=path[0]
            if len(seen)>app.max_nodes:_block('AX_TRAVERSAL_LIMIT')
            role=owner._get(ref,'AXRole');sub=owner._get(ref,'AXSubrole')
            if depth and role=='AXWindow':continue
            in_root=path[0]==root_index
            # No values, labels, identifiers, geometry or descendants of fields.
            if sub=='AXSecureTextField' or role in ('AXSecureTextField','AXTextField','AXTextArea','AXComboBox'):
                if form is not None:
                    form['secure_field_count' if sub=='AXSecureTextField' or role=='AXSecureTextField' else 'field_count']+=1
                continue
            if role in ('AXWindow','AXSheet','AXDialog'):
                form={'role':role,'path':path,'identifier':identifier(ref),'field_count':0,'secure_field_count':0,'labels':[],'signature':False}
                forms.append(form)
                if is_modal(owner._get,ref,role,sub):modal=True;in_modal=True
            if role in ('AXButton','AXStaticText','AXCheckBox'):
                attrs=('AXTitle','AXDescription','AXHelp','AXValue')
                passive_modal=(narrow or not opening) and in_modal
                values=[owner._get(ref,a) if not (passive_modal and (role=='AXStaticText' and path[-1] not in (10,14) or a in ('AXDescription','AXHelp') or a=='AXValue' and not (role=='AXStaticText' and path[-1] in (10,14)))) else None for a in attrs]
                known=sorted({v for v in values if type(v) is str and v in fixed})
                if in_root:
                    for label in known:
                        if label!='天府证券' or (role=='AXStaticText' and not in_modal):counts[label]+=1
                if any(marker in v for v in values if type(v) is str for marker in owner.profile['forbidden_text']):unsafe=True
                if form is not None:form['labels']=sorted(set(form['labels'])|set(known))
                if in_root and not after_press and role=='AXButton' and '立即登录' in known:
                    actions=app.backend.actions(ref) or []
                    facts={'role':role,'identifier':identifier(ref),'labels':known,'exact_attributes':[a for a,v in zip(attrs,values) if type(v) is str and v=='立即登录'],
                        'enabled':owner._get(ref,'AXEnabled') is True,'actions':['AXPress'] if 'AXPress' in actions else [],
                        'path':path,'geometry':geometry(ref),'locator':'EXACT_NATIVE_LABEL','in_modal':in_modal}
                    entries.append((ref,facts))
            if role not in containers:continue
            if role=='AXList' and depth!=1:continue
            children=owner._get(ref,'AXChildren') or []
            if not isinstance(children,(list,tuple)) or len(children)>app.max_nodes:_block('AX_TRAVERSAL_LIMIT')
            children=list(children)
            for relation in (('AXSheets',) if role in ('AXWindow','AXSheet') else ()):
                extra=owner._get(ref,relation) or []
                if not isinstance(extra,(list,tuple)) or len(extra)>12:_block('AX_TRAVERSAL_LIMIT')
                children.extend(extra)
            if depth>=app.max_depth and children:_block('AX_TRAVERSAL_LIMIT')
            queue.extend((child,depth+1,path+[i],form,in_modal) for i,child in enumerate(children))
        for form in forms:
            form['signature']=bool(form['secure_field_count'] or (form['field_count'] and '登录' in form['labels'] and set(form['labels']) & {'账号','资金账号','密码','交易密码','验证码'}))
        # Parent-confirmed paired layout, never a guessed index/pixel click.
        if not after_press and not entries and not modal:
            children=owner._get(root,'AXChildren') or []
            origin=geometry(root);pair=[]
            if isinstance(children,(list,tuple)) and len(children)>15 and origin is not None:
                for index,ident,y in ((13,'_NS:14',126),(15,'_NS:43',231)):
                    ref=children[index]
                    if owner._get(ref,'AXRole')!='AXButton' or owner._get(ref,'AXSubrole')=='AXSecureTextField':break
                    g=geometry(ref)
                    if identifier(ref)!=ident or g is None:break
                    if any(abs(a-b)>2 for a,b in zip((g['x']-origin['x'],g['y']-origin['y'],g['width'],g['height']),(73,y,168,28))):break
                    pair.append((ref,g))
                if len(pair)==2 and app.backend.key(pair[0][0])!=app.backend.key(pair[1][0]):
                    ref,g=pair[1];actions=app.backend.actions(ref) or []
                    entries.append((ref,{'role':'AXButton','identifier':'_NS:43','labels':['立即登录'],'exact_attributes':[],
                        'enabled':owner._get(ref,'AXEnabled') is True,'actions':['AXPress'] if 'AXPress' in actions else [],
                        'path':[next(i for i,w in enumerate(windows) if app.backend.key(w)==app.backend.key(root)),15],
                        'geometry':g,'locator':'USER_CONFIRMED_LOGIN_CHILD_15','in_modal':False,'associated_label':True,'pair_geometry':[g for _,g in pair]}))
        return entries,forms,counts,modal,unsafe,root_index
    def logged_in(forms,counts,modal,unsafe):
        if counts['天府证券']!=1 or modal or unsafe or any(f['signature'] for f in forms):return False
        if app.backend.key(session.scope().root)!=app.backend.key(root):return False
        try:
            return (session.detect()['page'] in ('持仓','委托','成交')
                    and app.backend.key(session.scope().root)==app.backend.key(root))
        except Exception as exc:
            from .._ax.errors import ElementNotFound,AmbiguousMatch
            if not isinstance(exc,(ElementNotFound,AmbiguousMatch)):raise
            return False
    return app, session, identity, root, entry_front, assert_binding, sample, logged_in


def _entry_report(entries, forms, counts):
    result={'status':'LOGIN_ENTRY_INSPECTED','press_attempted':False,'execution_enabled':False,
        'login_control_count':len(entries),'login_controls':[f for _,f in entries],
        'auth_forms':forms,'known_labels':counts,'already_logged_in':False,
        'authentication_unsupported':['SECRET_READ','FIELD_FILL','LOGIN_SUBMIT','AUTO_RESTORE','PERSISTENT_MONITOR']}
    return result


def status(owner, budget, *, inspect=False, narrow=False, binding=None):
    app,session,identity,root,_,assert_binding,sample,logged_in = _probe(owner,narrow=narrow,binding=binding)
    entries,forms,counts,_,_,_ = sample()
    result = _entry_report(entries,forms,counts)
    from .login_result import observe
    def current_logged():
        _,current_forms,current_counts,current_modal,current_unsafe,_=sample()
        return logged_in(current_forms,current_counts,current_modal,current_unsafe)
    outcome=observe(owner,budget,app,session,identity,root,current_logged,assert_binding=assert_binding)
    if inspect or outcome['status'] in ('LOGIN_COMMUNICATION_ERROR','ALREADY_LOGGED_IN','LOGIN_RESULT_UNRECOGNIZED'):
        return {**result,**outcome,'already_logged_in':outcome['authenticated']}
    return {**result,'already_logged_in':False}
