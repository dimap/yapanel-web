"""Routing/authentication/tunnel regression tests with isolated fake workers."""
import http.client,json,socket,sys,tempfile,threading,unittest,subprocess
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import multi as gateway,access,server
from unittest.mock import Mock, patch
gateway.DEVICES.update({name:dict(name=name,model=name) for name in ("station2","mini3pro")})

class Worker(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):self.do_GET()
    def do_GET(self):
        if self.headers.get('X-Panel-Gateway')!='test-gateway-secret':
            self.send_error(403);return
        if self.headers.get('Upgrade','').lower()=='websocket':
            self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.end_headers();self.wfile.flush()
            self.connection.sendall(self.connection.recv(8));return
        if self.path=='/':
            data=b'<head></head><script>fetch("/api/status");new WebSocket(`ws://${location.host}/api/term`)</script>'
            kind='text/html'
        else:
            size=int(self.headers.get('Content-Length',0));body=self.rfile.read(size) if size else b''
            data=json.dumps({'model':self.server.model,'path':self.path,'body':body.decode()}).encode();kind='application/json'
        self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

class TestGateway(gateway.Gateway):
    __test__ = False
    def _allowed(self,path):return not server.Handler._guard(self,path)

class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.servers=[]
        for model in gateway.DEVICES:
            worker=ThreadingHTTPServer(('127.0.0.1',0),Worker);worker.model=model
            gateway.DEVICES[model]['port']=worker.server_port;cls.servers.append(worker)
        TestGateway.data_dir=Path(cls.tmp.name);TestGateway.secret='test-gateway-secret';TestGateway.access=access.PanelAccess(Path(cls.tmp.name)/'auth.json')
        token=TestGateway.access.set_password('','unit-test-password','unittest')
        cls.cookie=access.COOKIE+'='+token
        cls.gateway=ThreadingHTTPServer(('127.0.0.1',0),TestGateway);cls.servers.append(cls.gateway)
        cls.host='127.0.0.1:'+str(cls.gateway.server_port)
        for item in cls.servers:threading.Thread(target=item.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for item in cls.servers:item.shutdown();item.server_close()
        cls.tmp.cleanup()
    def request(self,path,method='GET',cookie=True,origin=None,body=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=5)
        headers={'Cookie':self.cookie}if cookie else {}
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/octet-stream'
        connection.request(method,path,body=body,headers=headers);response=connection.getresponse();data=response.read();status=response.status;head=dict(response.getheaders());connection.close();return status,data,head
    def test_unauthenticated_cannot_read_or_write_either_device(self):
        for model in gateway.DEVICES:
            for method in ['GET','POST']:
                self.assertEqual(self.request('/speaker/'+model+'/api/status',method,cookie=False)[0],401)
    def test_updates_are_owned_by_gateway_and_keep_auth_and_csrf(self):
        TestGateway.updater=Mock()
        TestGateway.updater.check.return_value={'version':'0.1.3','latest':'0.1.4','update':True}
        TestGateway.updater.update.return_value={'ok':True,'version':'0.1.4'}
        status,data,_=self.request('/speaker/station2/api/panel/version?refresh=1')
        self.assertEqual(status,200);self.assertTrue(json.loads(data)['multi'])
        TestGateway.updater.check.assert_called_once_with(True)
        self.assertEqual(self.request('/speaker/station2/api/panel/update','POST',cookie=False)[0],401)
        self.assertEqual(self.request('/speaker/station2/api/panel/update','POST',origin='https://untrusted.example')[0],403)
        self.assertEqual(self.request('/speaker/station2/api/panel/update','POST',origin='null')[0],403)
        TestGateway.updater.update.assert_not_called()
        self.assertEqual(self.request('/speaker/mini3pro/api/panel/update','POST')[0],200)
        TestGateway.updater.update.assert_called_once_with()
    def test_paths_pin_requests_to_device_even_with_concurrent_tabs(self):
        for _ in range(2):
            for model in ['station2','mini3pro']:
                status,data,_=self.request('/speaker/'+model+'/api/shell','POST',body='read-only-test')
                self.assertEqual(status,200);value=json.loads(data);self.assertEqual(value['model'],model);self.assertEqual(value['path'],'/api/shell');self.assertEqual(value['body'],'read-only-test')
    def test_csrf_denied(self):
        self.assertEqual(self.request('/speaker/mini3pro/api/shell','POST',origin='https://untrusted.example',body='test')[0],403)
    def test_proxy_preserves_page_bytes_instead_of_rewriting_source(self):
        for model in gateway.DEVICES:
            status,data,_=self.request('/speaker/'+model+'/');self.assertEqual(status,200)
            self.assertEqual(data,b'<head></head><script>fetch("/api/status");new WebSocket(`ws://${location.host}/api/term`)</script>')
    def test_invalid_and_implicit_routes_cannot_write(self):
        for path in ['/speaker/unknown/api/shell','/api/shell','/speaker/station2','https://example.com/speaker/station2/api/shell']:
            self.assertEqual(self.request(path,'POST',body='test')[0],409)
    def test_one_auth_session_for_both_devices(self):
        for model in gateway.DEVICES:
            status,data,_=self.request('/speaker/'+model+'/api/panel-auth');self.assertEqual(status,200);self.assertTrue(json.loads(data)['authed'])
    def test_manifest_uses_device_name_and_relative_scope(self):
        for model in gateway.DEVICES:
            status,data,_=self.request('/speaker/'+model+'/manifest.webmanifest')
            self.assertEqual(status,200);manifest=json.loads(data)
            self.assertEqual(manifest['name'],gateway.DEVICES[model]['name'])
            self.assertEqual(manifest['scope'],'./')
            self.assertTrue(all(icon['src'].startswith('./static/')for icon in manifest['icons']))
    def test_websocket_origin_and_login_are_checked(self):
        for cookie,origin,expected in [(False,'http://'+self.host,401),(True,'https://untrusted.example',403)]:
            connection=http.client.HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=5)
            headers={'Origin':origin,'Upgrade':'websocket','Connection':'Upgrade'}
            if cookie:headers['Cookie']=self.cookie
            connection.request('GET','/speaker/mini3pro/api/term',headers=headers);response=connection.getresponse();self.assertEqual(response.status,expected);response.read();connection.close()
    def test_authenticated_websocket_tunnel_transfers_bytes(self):
        with socket.create_connection(('127.0.0.1',self.gateway.server_port),timeout=5)as sock:
            packet=f'GET /speaker/mini3pro/api/term HTTP/1.1\r\nHost: {self.host}\r\nOrigin: http://{self.host}\r\nCookie: {self.cookie}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n'
            sock.sendall(packet.encode());reply=b''
            while b'\r\n\r\n'not in reply:reply+=sock.recv(1024)
            self.assertIn(b' 101 ',reply);sock.sendall(b'tunnel!!');self.assertEqual(sock.recv(8),b'tunnel!!')
    def test_private_worker_accepts_signed_snat_peer_only_on_loopback_listener(self):
        code='''import os,server,json
from types import SimpleNamespace
os.environ['PANEL_GATEWAY_SECRET']='test-secret'
results=[]
for local,header in [('127.0.0.1','test-secret'),('127.0.0.1','wrong'),('192.0.2.1','test-secret')]:
 req=SimpleNamespace(client_address=('192.0.2.2',4321),connection=SimpleNamespace(getsockname=lambda:(local,18765)),headers={'X-Panel-Gateway':header},_send=lambda *a:None)
 results.append(server.Handler._guard(req,'/api/status'))
print(json.dumps(results))'''
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout),[False,True,True])



class AutomaticHTTPSTests(unittest.TestCase):
    def test_default_start_generates_certificate_and_explicit_zero_disables_it(self):
        import contextlib,io
        for options, enabled in [([], True), (['--https-port','0'],False)]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                http=Mock();http.server_port=8765
                tls=Mock();tls.server_port=8766
                workers=Mock();workers.start.return_value='test-secret'
                cert=Path(directory)/'server.crt';key=Path(directory)/'server.key'
                with patch.object(sys,'argv',['multi.py','--config','unused','--data-dir',directory]+options), \
                     patch.object(gateway,'load_config',return_value={}), \
                     patch.object(gateway,'Workers',return_value=workers), \
                     patch.object(gateway,'ThreadingHTTPServer',return_value=http), \
                     patch.object(gateway.server,'TLSServer',return_value=tls) as listener, \
                     patch.object(gateway.access,'ensure_certs',return_value=(cert,key)) as ensure, \
                     patch.object(gateway.ssl,'SSLContext') as context, \
                     patch.object(gateway.threading,'Thread'), \
                     patch.object(gateway.signal,'signal'), contextlib.redirect_stdout(io.StringIO()):
                    gateway.main()
                    if enabled:
                        ensure.assert_called_once_with(Path(directory).resolve()/'.tls')
                        context.return_value.load_cert_chain.assert_called_once_with(cert,key)
                        self.assertEqual(listener.call_args.args[0],('127.0.0.1',8766))
                    else:ensure.assert_not_called();listener.assert_not_called()
                workers.stop.assert_called_once()

class HTTPSRecoveryTests(unittest.TestCase):
    def test_tls_failures_keep_http_running_and_busy_port_retries(self):
        import contextlib, io, ssl
        cases = [
            ('missing-openssl', access.AccessError('OpenSSL missing'), None, 0, False),
            ('certificate-write', OSError('cannot write certificate'), None, 0, False),
            ('certificate-load', None, ssl.SSLError('invalid certificate'), 0, False),
            ('port-occupied', None, None, 20, False),
            ('port-released', None, None, 2, True),
        ]
        for name, cert_error, load_error, failures, enabled in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                http = Mock(server_port=8765)
                tls = Mock(server_port=8766)
                workers = Mock()
                output = io.StringIO()
                def serving_http():
                    # Fallback serves HTTP before cleaning up its workers.
                    workers.stop.assert_not_called()
                    self.assertEqual('https' in gateway.Gateway.ports, enabled)
                http.serve_forever.side_effect = serving_http
                with patch.object(sys, 'argv', ['multi.py', '--config', 'unused', '--data-dir', directory]), \
                     patch.object(gateway, 'load_config', return_value={}), \
                     patch.object(gateway, 'Workers', return_value=workers), \
                     patch.object(gateway, 'ThreadingHTTPServer', return_value=http), \
                     patch.object(gateway.server, 'TLSServer', side_effect=[OSError('port busy')] * failures + ([tls] if enabled else [])) as listener, \
                     patch.object(gateway.access, 'ensure_certs', return_value=('cert', 'key'), side_effect=cert_error), \
                     patch.object(gateway.ssl, 'SSLContext') as context, \
                     patch.object(gateway.time, 'sleep') as sleep, \
                     patch.object(gateway.threading, 'Thread'), \
                     patch.object(gateway.signal, 'signal'), contextlib.redirect_stdout(output):
                    context.return_value.load_cert_chain.side_effect = load_error
                    gateway.main()
                http.serve_forever.assert_called_once()
                workers.stop.assert_called_once()
                http.server_close.assert_called_once()
                self.assertEqual(listener.call_count, failures + int(enabled))
                self.assertEqual(sleep.call_count, min(failures, 19))
                self.assertEqual('панель работает по HTTP' in output.getvalue(), not enabled)
                if enabled:
                    tls.shutdown.assert_called_once()
                    tls.server_close.assert_called_once()

if __name__=='__main__':unittest.main(verbosity=2)
