"""One authenticated LAN console, explicit per-device routes, private loopback workers."""
import http.client
import ipaddress
import json
import os
import select
import socket
import threading
import argparse
import re
import secrets
import signal
import ssl
import subprocess
import sys
import tempfile
import time
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import access
import server

DEVICES = {}
ROOT = Path(__file__).resolve().parent
HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','content-length','server','date','set-cookie','proxy-connection'}
MAX_BODY=512*1024*1024
PROXY_TIMEOUT=180
TUNNEL_TIMEOUT=15
COOKIE_RE=re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}$")

def cookie_name():
    """Session cookie name shared by the gateway and its private workers."""
    try:
        from config import get_config
        name=get_config().security.cookie_name
    except Exception:  # broken settings must not stop the panel: fall back to access defaults
        name=None
    if isinstance(name,str) and COOKIE_RE.fullmatch(name):return name
    fallback=getattr(access,'COOKIE','yl2_session')
    return fallback if isinstance(fallback,str) and COOKIE_RE.fullmatch(fallback) else 'yl2_session'

access.COOKIE=cookie_name()

def network(value):
    """Parse --allow-network without accepting a host address as a prefix by accident."""
    try:return ipaddress.ip_network(value,strict=False)
    except ValueError as error:raise argparse.ArgumentTypeError(f'некорректная сеть {value!r}: {error}') from error

def hop_headers(headers):
    """HOP plus any extra hop-by-hop names advertised by Connection: ..."""
    names=set(HOP)
    get_all=getattr(headers,'get_all',None)
    values=get_all('Connection') if get_all else [headers.get('Connection','')]
    for value in values or []:
        names.update(token.strip().lower() for token in value.split(',') if token.strip())
    return names

def route(path):
    parsed=urlsplit(path)
    if parsed.scheme or parsed.netloc:raise ValueError('Требуется локальный путь')
    parts=parsed.path.split('/',3)
    if len(parts)<4 or parts[1]!='speaker' or parts[2] not in DEVICES:
        raise ValueError('Неизвестная колонка')
    model=parts[2]
    return model,'/'+parts[3]+('?' + parsed.query if parsed.query else '')

class Gateway(server.Handler):
    secret=''
    ports = {}
    networks = []
    data_dir = None
    def _panel_auth_get(self):
        return {**self.access.info(self._token()), 'secure':self.https, 'ports':self.ports, 'ca':(self.data_dir/'.tls/ca.crt').exists()}

    def _allowed(self,path):
        if self.networks:
            address=self._client_ip()
            if address is None or not self._network_ok(address):
                self._send(403,{'error':'Доступ только из домашней сети'});return False
        return not server.Handler._guard(self,path)

    def _client_ip(self):
        try:return ipaddress.ip_address(str(self.client_address[0]))
        except (AttributeError,IndexError,TypeError,ValueError):return None

    def _network_ok(self,address):
        candidates=[address]
        if address.version==6 and address.ipv4_mapped is not None:candidates.append(address.ipv4_mapped)
        for network in self.networks:
            try:
                parsed=ipaddress.ip_network(network,strict=False) if isinstance(network,str) else network
            except (TypeError,ValueError):
                continue
            if not isinstance(parsed,(ipaddress.IPv4Network,ipaddress.IPv6Network)):continue
            if any(candidate.version==parsed.version and candidate in parsed for candidate in candidates):return True
        return False

    def do_GET(self):self.dispatch()
    def do_POST(self):self.dispatch()
    def do_HEAD(self):self._send(405,{'error':'Метод не поддерживается'})

    def dispatch(self):
        try:
            try:length=self._content_length()
            except ValueError as error:return self._send(400,{'error':str(error)})
            if length>MAX_BODY:return self._send(413,{'error':'Слишком большой запрос'})
            if self.path=='/' or self.path.startswith('/?'):
                if not self._allowed('/'):return
                cookies=SimpleCookie()
                try:cookies.load(self.headers.get('Cookie',''))
                except Exception:pass
                selected=cookies.get('panel_device')
                model=selected.value if selected and selected.value in DEVICES else next(iter(DEVICES))
                return self._send(302,b'',headers=[('Location','/speaker/'+model+'/')])
            try:model,path=route(self.path)
            except ValueError:
                return self._send(409,{'error':'Обновите страницу единой панели','url':'/'})
            clean=urlsplit(path).path
            if not self._allowed(clean):return
            if clean=='/api/panel-auth' and self.command=='GET':
                return self._send(200,self._panel_auth_get())
            if clean.startswith('/api/panel-auth/') and self.command=='POST':
                return self._panel_auth_post(clean,self._json())
            if clean=='/api/speakers' and self.command=='GET':
                return self._send(200,{'selected':model,'devices':[{'id':key,'name':value['name']}for key,value in DEVICES.items()]})
            if clean=='/manifest.webmanifest' and self.command=='GET':
                manifest=json.loads((server.STATIC/'manifest.webmanifest').read_text(encoding='utf8'))
                manifest.update(name=DEVICES[model]['name'],short_name=DEVICES[model]['name'])
                return self._send(200,json.dumps(manifest,ensure_ascii=False).encode('utf8'),server.MIME['.webmanifest'])
            if clean == '/api/panel/version' and self.command == 'GET':
                return self._handle(lambda: {**self.updater.check('refresh=1' in urlsplit(path).query.split('&')), 'multi': True})
            if clean == '/api/panel/update' and self.command == 'POST':
                return self._handle(self.updater.update)
            if clean == '/ca.crt':
                certificate = self.data_dir/'.tls/ca.crt'
                if not certificate.exists(): return self._send(404, {'error':'HTTPS не включён'})
                return self._send(200,certificate.read_bytes(),'application/x-x509-ca-cert',headers=[('Content-Disposition','attachment; filename="stantsiya-panel-ca.crt"')])
            if self.headers.get('Upgrade','').lower()=='websocket':
                return self.tunnel(model,path)
            self.proxy(model,path)
        except (BrokenPipeError,ConnectionResetError):pass
        except (OSError,http.client.HTTPException,ValueError) as error:
            self._send(502,{'error':'Не удалось связаться с выбранной колонкой','detail':str(error)[:180]})

    def request_headers(self):
        hop=hop_headers(self.headers)
        return {**{key:value for key,value in self.headers.items() if key.lower() not in hop and key.lower()!='x-panel-gateway'},'X-Panel-Gateway':self.secret}

    def _content_length(self):
        get_all=getattr(self.headers,'get_all',None)
        values=get_all('Content-Length') if get_all else None
        if values is None:
            single=self.headers.get('Content-Length');values=[] if single is None else [single]
        if not values:return 0
        normalized={value.strip() for value in values}
        if len(normalized)!=1:raise ValueError('Конфликтующие Content-Length')
        raw=normalized.pop()
        if not raw.isdigit():raise ValueError('Некорректный Content-Length')
        return int(raw)

    def _read_body(self,length):
        chunks=[];left=length
        while left:
            chunk=self.rfile.read(min(left,65536))
            if not chunk:return None
            chunks.append(chunk);left-=len(chunk)
        return b''.join(chunks)

    def proxy(self,model,path):
        try:length=self._content_length()
        except ValueError as error:return self._send(400,{'error':str(error)})
        if length>MAX_BODY:return self._send(413,{'error':'Слишком большой запрос'})
        if self.headers.get('Transfer-Encoding'):
            return self._send(400,{'error':'Transfer-Encoding не поддерживается'})
        try:self.connection.settimeout(PROXY_TIMEOUT)
        except OSError:pass
        body=b''
        if length:
            try:body=self._read_body(length)
            except TimeoutError:
                self.close_connection=True;return self._send(408,{'error':'Истекло время ожидания тела запроса'})
            except (OSError,ValueError):
                self.close_connection=True;return
            if body is None:
                self.close_connection=True;return
        conn=http.client.HTTPConnection('127.0.0.1',DEVICES[model]['port'],timeout=PROXY_TIMEOUT)
        responded=False
        try:
            headers=self.request_headers()
            if self.headers.get('Content-Length') is not None:headers['Content-Length']=str(length)
            conn.request(self.command,path,body=body if length else None,headers=headers)
            response=conn.getresponse()
            responded=True
            drop=hop_headers(response.msg)
            self.send_response(response.status)
            for key,value in response.getheaders():
                lower=key.lower()
                if lower in drop or lower in ('content-length','x-panel-gateway'):continue
                self.send_header(key,value)
            if urlsplit(path).path in ('/','/index.html'):
                self.send_header('Set-Cookie',f'panel_device={model}; Path=/; SameSite=Strict; Max-Age=31536000')
            # http.client уже распаковал chunked, поэтому здесь важен только честный Content-Length.
            if not response.chunked:
                size=response.getheader('Content-Length')
                if size is not None:self.send_header('Content-Length',size)
            self.send_header('Connection','close');self.end_headers();self.close_connection=True
            if self.command!='HEAD':
                while chunk:=response.read(65536):self.wfile.write(chunk)
        except TimeoutError:
            if responded:self.close_connection=True
            else:self._send(504,{'error':'Колонка не ответила вовремя'})
        except (OSError,http.client.HTTPException) as error:
            if responded:self.close_connection=True
            else:self._send(502,{'error':'Не удалось связаться с выбранной колонкой','detail':str(error)[:180]})
        finally:conn.close()

    def _origin_ok(self):
        origin=self.headers.get('Origin');host=self.headers.get('Host')
        if not origin or origin=='null' or not host:return False
        try:
            source=urlsplit(origin);target=urlsplit('//'+host)
            defaults={'http':80,'https':443,'ws':80,'wss':443};scheme=source.scheme.lower()
            if source.username or source.password or target.username or target.password:return False
            if not source.hostname or not target.hostname or scheme not in defaults:return False
            return source.hostname.lower()==target.hostname.lower() and (source.port or defaults[scheme])==(target.port or defaults[scheme])
        except ValueError:return False

    @staticmethod
    def _clean_handshake(data):
        """Never relay the private worker key if a worker echoes request headers."""
        head,separator,rest=data.partition(b'\r\n\r\n');lines=head.split(b'\r\n')
        if len(lines)>1:lines=[lines[0]]+[line for line in lines[1:] if not line.lower().startswith(b'x-panel-gateway:')]
        return b'\r\n'.join(lines)+separator+rest

    def tunnel(self,model,path):
        # Authenticate before connecting and keep the selected worker fixed for the whole socket.
        if not self._origin_ok():
            return self._send(403,{'error':'Запрос консоли с чужого сайта'})
        if any(ord(character)<32 or ord(character)==127 or character==' ' for character in path):
            return self._send(400,{'error':'Некорректный путь'})
        try:upstream=socket.create_connection(('127.0.0.1',DEVICES[model]['port']),timeout=TUNNEL_TIMEOUT)
        except TimeoutError:return self._send(504,{'error':'Колонка не ответила вовремя'})
        except OSError as error:return self._send(502,{'error':'Не удалось связаться с выбранной колонкой','detail':str(error)[:180]})
        started=False
        with upstream:
            try:
                upstream.settimeout(TUNNEL_TIMEOUT)
                headers=self.request_headers();headers.update({'Upgrade':'websocket','Connection':'Upgrade'})
                packet=f'GET {path} HTTP/1.1\r\n'+''.join(f'{key}: {value}\r\n'for key,value in headers.items())+'\r\n'
                upstream.sendall(packet.encode('latin1'))
                received=b''
                while b'\r\n\r\n' not in received:
                    chunk=upstream.recv(4096)
                    if not chunk:raise OSError('Консоль закрыла соединение')
                    received+=chunk
                    if len(received)>65536:raise OSError('Некорректный ответ консоли')
                status=received.split(b'\r\n',1)[0]
                received=self._clean_handshake(received)
                self.wfile.write(received);self.wfile.flush();started=True;self.close_connection=True
                if b' 101 ' not in status:return
                upstream.settimeout(None)
                while True:
                    if not self.access.session_ok(self._token()):return
                    ready,_,_=select.select([self.connection,upstream],[],[],5)
                    for source in ready:
                        chunk=source.recv(65536)
                        if not chunk:return
                        (upstream if source is self.connection else self.connection).sendall(chunk)
            except TimeoutError:
                if started:self.close_connection=True
                else:self._send(504,{'error':'Колонка не ответила вовремя'})
            except (OSError,ValueError) as error:
                if started:self.close_connection=True
                else:self._send(502,{'error':'Не удалось связаться с выбранной колонкой','detail':str(error)[:180]})


def load_config(path):
    from profiles import PROFILES
    value = json.loads(Path(path).read_text(encoding='utf8'))
    speakers = value.get('speakers') if isinstance(value,dict) else None
    if not isinstance(speakers,list) or not 1 <= len(speakers) <= 32:
        raise ValueError('speakers must contain 1..32 devices')
    result = {}
    for item in speakers:
        if not isinstance(item,dict): raise ValueError('invalid speaker')
        identifier = item.get('id','')
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,39}',identifier) or identifier in result:
            raise ValueError('speaker ids must be unique lowercase URL-safe names')
        model = item.get('model','lite2')
        if model not in PROFILES: raise ValueError('unknown model')
        device = item.get('device')
        if not isinstance(device,str) or not device.strip() or any(character.isspace() for character in device):
            raise ValueError('device must be an ADB address or USB serial')
        name = item.get('name',PROFILES[model]['name'])
        if not isinstance(name,str) or not 1 <= len(name) <= 80: raise ValueError('invalid speaker name')
        result[identifier] = {'name':name,'model':model,'device':device}
    return result


class Workers:
    def __init__(self,config,data_dir,timeout=30,cookie_name=None):
        self.config,self.data_dir,self.timeout = config,data_dir,timeout
        self.cookie_name=cookie_name
        self.children=[]
    def start(self):
        secret=secrets.token_urlsafe(48)
        try:
            with tempfile.TemporaryDirectory(prefix='panel-ready-') as temporary:
                for identifier,spec in self.config.items():
                    directory=self.data_dir/'speakers'/identifier
                    directory.mkdir(parents=True,mode=0o700,exist_ok=True)
                    ready=Path(temporary)/(identifier+'.json')
                    env={**os.environ,'PANEL_GATEWAY_SECRET':secret,'PANEL_READY_FILE':str(ready)}
                    if self.cookie_name:
                        # Both aliases win over any inherited value, including PANEL_SESSION_COOKIE.
                        env['PANEL_COOKIE_NAME']=env['PANEL_SESSION_COOKIE']=self.cookie_name
                    command=[sys.executable,str(ROOT/'server.py'),'--device',spec['device'],'--model',spec['model'],
                             '--host','127.0.0.1','--port','0','--https-port','0','--data-dir',str(directory)]
                    log=open(directory/'worker.log','ab')
                    try: process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
                    finally: log.close()
                    self.children.append(process)
                    deadline=time.monotonic()+self.timeout
                    while not ready.exists():
                        if process.poll() is not None: raise RuntimeError('worker failed: '+identifier+'; see worker.log')
                        if time.monotonic()>deadline: raise RuntimeError('worker startup timeout: '+identifier)
                        time.sleep(.05)
                    spec['port']=json.loads(ready.read_text())['port']
            return secret
        except BaseException:
            self.stop();raise
    def stop(self):
        for child in self.children:
            if child.poll() is None: child.terminate()
        for child in self.children:
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill();child.wait()
        self.children.clear()


def restart_workers(workers):
    """Stop every private child before replacing the gateway process."""
    workers.stop()
    server.restart_self()


def main():
    parser=argparse.ArgumentParser(description='One console for several rooted Yandex speakers')
    parser.add_argument('--config',required=True,help='JSON speaker list; see speakers.example.json')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data/multi')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--https-port',type=int,default=8766,help='HTTPS port; certificate is generated automatically; 0 disables HTTPS')
    parser.add_argument('--allow-network',type=network,action='append',default=[],help='optional client CIDR; repeat for multiple networks')
    args=parser.parse_args()
    # Workers receive their private key in a copied environment; the gateway never uses worker mode.
    if os.environ.get('PANEL_GATEWAY_SECRET'): parser.error('PANEL_GATEWAY_SECRET is reserved for private workers')
    Gateway.networks=list(args.allow_network)
    config=load_config(args.config);DEVICES.update(config)
    access.COOKIE=cookie_name()
    data_dir=args.data_dir.resolve();data_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
    Gateway.data_dir=data_dir;Gateway.access=access.PanelAccess(data_dir/'.panel_auth.json')
    workers=Workers(config,data_dir,cookie_name=access.COOKIE)
    httpd=tls=None
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    try:
        Gateway.updater=server.PanelUpdater(restart=lambda: restart_workers(workers))
        Gateway.secret=workers.start()
        httpd=ThreadingHTTPServer((args.host,args.port),Gateway);httpd.daemon_threads=True
        Gateway.ports={'http':httpd.server_port}
        if args.https_port:
            try:
                certificate,key=access.ensure_certs(data_dir/'.tls')
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(certificate,key)
                for attempt in range(20):
                    try:
                        tls=server.TLSServer((args.host,args.https_port),Gateway,context)
                        break
                    except OSError:
                        if attempt == 19: raise
                        time.sleep(0.5)
                tls.daemon_threads=True
                threading.Thread(target=tls.serve_forever,daemon=True).start()
                Gateway.ports['https']=tls.server_port
            except (OSError,access.AccessError,ssl.SSLError) as error:
                if tls: tls.server_close();tls=None
                print(f'HTTPS не запущен ({error}) — панель работает по HTTP',flush=True)
        print('Unified panel: http://'+args.host+':'+str(httpd.server_port),flush=True)
        if not Gateway.access.enabled:print('Set a shared panel password in Settings before exposing it to your LAN.',flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        if tls:tls.shutdown();tls.server_close()
        if httpd:httpd.server_close()
        workers.stop()


if __name__=='__main__':main()
