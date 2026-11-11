"""Bounded code-only snapshot command, executed on the owning desktop."""
import shlex

SCRIPT = r'''
import fcntl,os,pathlib,stat,subprocess,sys,time
gitdir,workdir=sys.argv[1:]
root=pathlib.Path(gitdir)
root.mkdir(parents=True,exist_ok=True)
for path in (root,*root.parents):
 if not stat.S_ISDIR(path.lstat().st_mode):raise RuntimeError('Snapshot ancestor is not a directory')
marker=root/'openbox-managed-v1'
try:
 with marker.open('x') as output:output.write('bounded-code-snapshot-v1\n')
except FileExistsError:
 if marker.is_symlink() or marker.read_text()!='bounded-code-snapshot-v1\n':raise RuntimeError('Snapshot marker changed')
lock=os.open(str(root.parent/'.snapshot.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
fcntl.flock(lock,fcntl.LOCK_EX)
def git(*args,input=None):
 return subprocess.run(['git','--git-dir='+gitdir,'--work-tree='+workdir,*args],
  input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,
  env={**os.environ,'GIT_LITERAL_PATHSPECS':'1'},timeout=30).stdout
def stored_bytes(path):
 if (path/'objects').is_symlink():raise RuntimeError('Snapshot object store is a symlink')
 total=0
 for base,dirs,names in os.walk(path/'objects',followlinks=False):
  dirs[:]=[d for d in dirs if not (pathlib.Path(base)/d).is_symlink()]
  for name in names:total+=(pathlib.Path(base)/name).lstat().st_size
 return total
try:
 # Only this internal index is reset; the worktree and the user's .git are
 # never changed. Listing after reset honors exclusions for old tracked media.
 git('read-tree','--empty')
 names=git('ls-files','--others','--exclude-standard','-z').split(b'\0')
 selected=[];total=0
 for name in names:
  if not name:continue
  path=pathlib.Path(workdir)/os.fsdecode(name)
  info=path.lstat()
  if info.st_size>2*1024*1024:continue
  selected.append(name);total+=info.st_size
  if len(selected)>10000 or total>64*1024*1024:
   raise RuntimeError('Code snapshot exceeds bounded input budget')
 if selected:git('add','--pathspec-from-file=-','--pathspec-file-nul',input=b'\0'.join(selected)+b'\0')
 tree=git('write-tree').decode().strip()
 git('update-ref','refs/openbox/'+str(time.time_ns()),tree)
 refs=git('for-each-ref','--format=%(refname)','refs/openbox/').decode().splitlines()
 cutoff=time.time_ns()-7*86400*1000000000
 obsolete=[r for r in refs if r not in refs[-100:] or int(r.rsplit('/',1)[-1])<cutoff]
 for ref in obsolete:git('update-ref','-d',ref)
 if obsolete:git('gc','--prune=now','--quiet')
 if stored_bytes(root)>512*1024*1024:
  refs=git('for-each-ref','--format=%(refname)','refs/openbox/').decode().splitlines()
  for ref in refs[:-2]:git('update-ref','-d',ref)
  git('gc','--prune=now','--quiet')
 # Bound all project histories together. The newest two code snapshots in
 # each store survive trimming. A sole current project is <=64 MiB per tree.
 stores=[p for p in root.parent.iterdir() if p.is_dir() and not p.is_symlink() and (p/'objects').is_dir()
  and not (p/'objects').is_symlink() and (p/'openbox-managed-v1').is_file()
  and not (p/'openbox-managed-v1').is_symlink() and (p/'openbox-managed-v1').read_text()=='bounded-code-snapshot-v1\n']
 if sum(stored_bytes(p) for p in stores)>2*1024*1024*1024:
  for p in stores:
   cmd=['git','--git-dir='+str(p)]
   refs=subprocess.check_output(cmd+['for-each-ref','--format=%(refname)','refs/openbox/']).decode().splitlines()
   for ref in refs[:-2]:subprocess.run(cmd+['update-ref','-d',ref],check=True,stdout=subprocess.DEVNULL,timeout=10)
   subprocess.run(cmd+['gc','--prune=now','--quiet'],check=True,stdout=subprocess.DEVNULL,timeout=30)
 print(tree)
finally:os.close(lock)
'''


def command(gitdir, workdir):
    return "python3 -c " + shlex.quote(SCRIPT) + " " + shlex.quote(gitdir) + " " + shlex.quote(workdir)
