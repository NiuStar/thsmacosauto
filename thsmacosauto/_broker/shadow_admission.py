"""Controlled SHADOW filesystem capabilities, not a same-UID sandbox."""
import os,stat,resource,errno
from contextlib import contextmanager
from pathlib import Path

def require(ok,code):
    if not ok:raise ValueError(code)
def literal(value):
    require(type(value) is str and value.startswith('/') and value!='/' and not any(x in ('','.','..') for x in value.split('/')[1:]),'PATH_NOT_CANONICAL')
    return Path(value)
def identity(s):return (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid)
def metadata(s):return identity(s)+(s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def filecheck(s,mode,limit):
    require(stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and 0<=s.st_size<=limit,'PRIVATE_FILE_BOUND')
    require(stat.S_IMODE(s.st_mode)==mode,'PRIVATE_MODE')

def descriptor_snapshot():
    # Scan the entire finite hard limit, not a guessed low-fd prefix. Refuse
    # unsupported capability sizes explicitly instead of ignoring high handles.
    hard=resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    require(hard!=resource.RLIM_INFINITY and 0<hard<=1048576,'FD_BOUND_UNSUPPORTED')
    result={}
    for fd in range(hard):
        try:result[fd]=identity(os.fstat(fd))
        except OSError as exc:
            if exc.errno!=errno.EBADF:raise
    return result
@contextmanager
def parent_chain(value):
    path=literal(value);fds=[];entries=[]
    try:
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for part in path.parts[1:-1]:
            before=os.stat(part,dir_fd=fd,follow_symlinks=False)
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(child)
            require(identity(before)==identity(os.fstat(child)),'PARENT_CHANGED')
            entries.append((fd,part,child,identity(before)));fd=child
        def check():
            for parent,name,child,pin in entries:
                require(identity(os.fstat(child))==pin==identity(os.stat(name,dir_fd=parent,follow_symlinks=False)),'PARENT_CHANGED')
        check();yield fd,path.name,check
    finally:
        for fd in reversed(fds):os.close(fd)
def read_private(value,limit=32768,readonly=False):
    # Never normalize strings at this boundary; Path input was already literal-validated.
    text=value if type(value) is str else str(value)
    with parent_chain(text) as (parent,name,check):
        ps=os.fstat(parent)
        require(ps.st_uid==os.getuid() and stat.S_IMODE(ps.st_mode) in (0o700,0o500),'PRIVATE_PARENT')
        admitted=os.stat(name,dir_fd=parent,follow_symlinks=False);mode=0o400 if readonly else 0o600
        filecheck(admitted,mode,limit)
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            before=os.fstat(fd);filecheck(before,mode,limit)
            require(metadata(before)==metadata(admitted),'SOURCE_CHANGED');check()
            raw=os.read(fd,limit+1);require(len(raw)<=limit,'FILE_SIZE_LIMIT')
            after=os.fstat(fd);filecheck(after,mode,limit);check()
            require(metadata(before)==metadata(after)==metadata(os.stat(name,dir_fd=parent,follow_symlinks=False)),'SOURCE_CHANGED')
            return raw
        finally:os.close(fd)

class ShadowStateAdmission:
    """Held dirfd and admitted inode set; explicit Python-only SHADOW capability.

    POSIX bounded fstat enumeration binds SQLite's newly opened real descriptor to
    the admitted inode before SQL. Rechecks bound constructor/write seams, but
    cannot formally exclude an unbounded hostile same-UID link between syscalls.
    Existing pending rows are never recovery-written in this scope.
    """
    def __init__(self,path):
        self.path=str(literal(path));self.context=parent_chain(self.path);self.fd=None;self.files={};self.sqlite_fds=[]
        try:
            parent,name,self.check_chain=self.context.__enter__()
            s=os.stat(name,dir_fd=parent,follow_symlinks=False)
            self.fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            self.parent,self.name,self.pin=parent,name,identity(s);self.check(allow_new=True)
            for name in os.listdir(self.fd):
                require(name in ('SHADOW_ONLY','owner.lock','journal.sqlite3','control.sqlite3'),'STATE_UNADMITTED_ENTRY')
                self.admit(name)
        except BaseException:
            self.close();raise
    def check(self,allow_new=False):
        self.check_chain();s=os.fstat(self.fd)
        require(identity(s)==self.pin==identity(os.stat(self.name,dir_fd=self.parent,follow_symlinks=False)),'STATE_IDENTITY_CHANGED')
        require(s.st_uid==os.getuid() and stat.S_IMODE(s.st_mode)==0o700,'PRIVATE_MODE')
        for name,(fd,pin) in self.files.items():
            fs=os.fstat(fd);filecheck(fs,0o600,16*1024*1024)
            require(identity(fs)==pin==identity(os.stat(name,dir_fd=self.fd,follow_symlinks=False)),'STATE_FILE_CHANGED')
        # SQLite rollback/WAL/SHM leftovers are not accepted in this narrow lane.
        for name in os.listdir(self.fd):
            if not allow_new and name not in self.files:
                require(bool(self.sqlite_fds) and name in ('journal.sqlite3-journal','control.sqlite3-journal'),'STATE_UNADMITTED_ENTRY')
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
                try:
                    s=os.fstat(fd);filecheck(s,0o600,16*1024*1024)
                    require(metadata(s)==metadata(os.stat(name,dir_fd=self.fd,follow_symlinks=False)),'STATE_SIDECAR_CHANGED')
                finally:os.close(fd)
    def admit(self,name,create=False):
        require('/' not in name and name not in ('.','..'),'STATE_MEMBER')
        if name in self.files:return self.files[name][0]
        flags=os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK
        if create:flags|=os.O_CREAT|os.O_EXCL
        fd=os.open(name,flags,0o600,dir_fd=self.fd)
        try:
            s=os.fstat(fd);filecheck(s,0o600,16*1024*1024)
            require(identity(s)==identity(os.stat(name,dir_fd=self.fd,follow_symlinks=False)),'STATE_FILE_CHANGED')
            self.files[name]=(fd,identity(s));return fd
        except BaseException:os.close(fd);raise
    def connect(self,path,**kwargs):
        import sqlite3
        self.check();name=Path(path).name
        if name not in self.files:self.admit(name)
        self.check();before=descriptor_snapshot()
        conn=sqlite3.connect(path,**kwargs)
        try:
            self.check();pin=self.files[name][1];opened=[]
            for fd,observed in descriptor_snapshot().items():
                if observed==pin and before.get(fd)!=pin:opened.append(fd)
            require(bool(opened),'SQLITE_DESCRIPTOR_NOT_BOUND')
            self.sqlite_fds.extend((fd,pin) for fd in opened)
            return GuardedConnection(conn,self)
        except BaseException:conn.close();raise
    def close(self):
        for fd,_pin in self.files.values():os.close(fd)
        self.files={}
        if self.fd is not None:os.close(self.fd);self.fd=None
        if getattr(self,'context',None) is not None:
            context,self.context=self.context,None;context.__exit__(None,None,None)

class GuardedConnection:
    def __init__(self,conn,guard):object.__setattr__(self,'conn',conn);object.__setattr__(self,'guard',guard)
    def __getattr__(self,name):return getattr(self.conn,name)
    def __setattr__(self,name,value):setattr(self.conn,name,value)
    def execute(self,sql,*args):
        # Every SQL entry is checked, including actual recovery and control writes.
        self.guard.check()
        for fd,pin in self.guard.sqlite_fds:require(identity(os.fstat(fd))==pin,'SQLITE_DESCRIPTOR_CHANGED')
        return self.conn.execute(sql,*args)
    def executescript(self,sql):
        self.guard.check();return self.conn.executescript(sql)
