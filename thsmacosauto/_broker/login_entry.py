"""Explicit login-dialog opening, separate from observing or submitting login."""
from .native_errors import _block
from .login_status import _entry_report, _probe
from .disconnect_warning import before_open


def open_dialog(owner, budget, *, narrow=False, binding=None):
    owner._owner_proof()
    refusal=before_open(owner,budget,explicit_navigation=True)
    if refusal is not None:return refusal
    app,session,identity,root,entry_front,_,sample,logged_in = _probe(owner,opening=True,narrow=narrow,binding=binding)
    entries,forms,counts,modal,unsafe,root_index=sample()
    result=_entry_report(entries,forms,counts)
    if logged_in(forms,counts,modal,unsafe):
        return {**result,'status':'ALREADY_LOGGED_IN','already_logged_in':True}
    if getattr(owner,'_login_entry_claimed',False):return {**result,'status':'LOGIN_ALREADY_CLAIMED'}
    if any(f['signature'] for f in forms):return {**result,'status':'LOGIN_FORM_ALREADY_PRESENT'}
    if modal or unsafe or counts['天府证券']:return {**result,'status':'LOGIN_ENTRY_BLOCKED'}
    if not owner.config.get('allow_navigation',False):return {**result,'status':'NAVIGATION_DISABLED'}
    if len(entries)!=1:return {**result,'status':'LOGIN_CONTROL_NOT_UNIQUE'}
    ref,facts=entries[0]
    if not facts['enabled'] or facts['actions']!=['AXPress'] or facts['in_modal']:return {**result,'status':'LOGIN_CONTROL_UNSAFE'}
    if owner._owned_effect(owner._runtime.app_identity)!=identity or app.backend.key(session.scope().root)!=app.backend.key(root):_block('NATIVE_CONTINUITY_CHANGED')
    fresh=sample()
    if fresh[1:]!=(forms,counts,modal,unsafe,root_index) or len(fresh[0])!=1 or app.backend.key(fresh[0][0][0])!=app.backend.key(ref) or fresh[0][0][1]!=facts:
        return {**result,'status':'LOGIN_ENTRY_BLOCKED'}
    if owner._owned_effect(owner._runtime.app_identity)!=identity or app.backend.key(session.scope().root)!=app.backend.key(root):_block('NATIVE_CONTINUITY_CHANGED')
    if owner._owned_effect(owner._runtime.front)!=entry_front:return {**result,'status':'LOGIN_TARGET_CHANGED'}
    if not owner._navigation_modal_clear(app,session,identity,root,entry_front):return {**result,'status':'LOGIN_ENTRY_BLOCKED'}
    owner._owner_proof();budget.check()
    owner._login_entry_claimed=True
    result.update(status='LOGIN_ENTRY_UNCONFIRMED',press_attempted=True)
    budget.event('LOGIN_ENTRY_CLAIMED')
    try:
        def dispatch():
            return owner._press_operator_control(ref)
        owner._owned_effect(dispatch)
        if owner._owned_effect(owner._runtime.app_identity)!=identity:return result
        _,after,_,_,after_unsafe,after_root_index=sample(after_press=True)
        result['auth_forms']=after
        if not after_unsafe and any(f['signature'] and f['path'][0]==after_root_index for f in after):
            result['status']='LOGIN_DIALOG_OPENED'
            owner._login_entry_claimed=False
    except Exception:
        return result
    return result
