typedef unsigned long size_t;
typedef long frames_t;
typedef unsigned long uframes_t;
extern void *dlsym(void *, const char *);
extern int open(const char *, int, ...);
extern long write(int, const void *, size_t);
extern int close(int);
extern int access(const char*,int);
struct area {void *addr; unsigned first, step;};
static int fd=-1,meta=-1; static unsigned written; static __thread int reading;
static __thread const struct area *areas; static __thread void *area_pcm;
static int (*streamfn)(void *); static long (*bytesfn)(void *,long);
static void init(void){
 if(!streamfn) streamfn=dlsym((void*)-1,"snd_pcm_stream");
 if(!bytesfn) bytesfn=dlsym((void*)-1,"snd_pcm_frames_to_bytes");
}
static int wanted(void *p){init(); return streamfn && streamfn(p)==1;}
static void save(void *p,const void *data,unsigned n,unsigned kind){
 if(!n || written>=4608000 || access("/tmp/codex-mic-tap.enable",0)!=0) return;
 if(fd==-1) {fd=open("/tmp/codex-mic-tap.raw",1|64|512|2048,0600); meta=open("/tmp/codex-mic-tap.meta",1|64|512|2048,0600);}
 if(fd<0) return;
 unsigned hdr[4]={kind,(unsigned)bytesfn(p,1),n,written};
 if(n>4608000-written)n=4608000-written;
 long r=write(fd,data,n); if(r>0){written+=(unsigned)r;if(meta>=0)write(meta,hdr,sizeof hdr);}
}
frames_t snd_pcm_readi(void *p,void *buf,uframes_t n){
 static frames_t (*fn)(void*,void*,uframes_t); if(!fn)fn=dlsym((void*)-1,"snd_pcm_readi");
 reading++; frames_t r=fn(p,buf,n); reading--; if(r>0 && !reading && wanted(p))save(p,buf,(unsigned)bytesfn(p,r),1);return r;
}
frames_t snd_pcm_mmap_readi(void *p,void *buf,uframes_t n){
 static frames_t (*fn)(void*,void*,uframes_t); if(!fn)fn=dlsym((void*)-1,"snd_pcm_mmap_readi");
 reading++; frames_t r=fn(p,buf,n); reading--; if(r>0 && !reading && wanted(p))save(p,buf,(unsigned)bytesfn(p,r),2);return r;
}
int snd_pcm_mmap_begin(void*p,const struct area **a,uframes_t *off,uframes_t *n){
 static int (*fn)(void*,const struct area**,uframes_t*,uframes_t*);if(!fn)fn=dlsym((void*)-1,"snd_pcm_mmap_begin");
 int r=fn(p,a,off,n);if(r==0&&!reading&&wanted(p)){areas=*a;area_pcm=p;}return r;
}
frames_t snd_pcm_mmap_commit(void*p,uframes_t off,uframes_t n){
 static frames_t (*fn)(void*,uframes_t,uframes_t);if(!fn)fn=dlsym((void*)-1,"snd_pcm_mmap_commit");
 if(!reading&&p==area_pcm&&areas&&n&&areas[0].first==0&&areas[0].step==(unsigned)bytesfn(p,1)*8)
  save(p,(char*)areas[0].addr+off*(areas[0].step/8),(unsigned)bytesfn(p,n),3);
 return fn(p,off,n);
}
