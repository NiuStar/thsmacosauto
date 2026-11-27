"""Controlled SHADOW filesystem capabilities, not a same-UID sandbox."""
import os,stat
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
        require(ps.st_uid==os.getuid() and stat.S_IMODE(ps.st_mode)==0o700,'PRIVATE_PARENT')
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
