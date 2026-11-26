import argparse
from pathlib import Path
import threading
import webbrowser
from ab_platform.http_service import configure_log,serve
from .service import AService


def main():
    parser=argparse.ArgumentParser(description='Independent PC A, no ESP32 required')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent/'.runtime')
    parser.add_argument('--port',type=int,default=8766);parser.add_argument('--open',action='store_true')
    args=parser.parse_args();configure_log(args.root);service=AService(args.root);server=serve(service,'127.0.0.1',args.port)
    print(f'PC A: http://127.0.0.1:{server.server_port}',flush=True)
    print('Administrator password: '+str(args.root.resolve()/'admin_password.txt'),flush=True)
    if args.open:threading.Timer(.5,lambda:webbrowser.open(f'http://127.0.0.1:{server.server_port}')).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();service.close()


if __name__=='__main__':main()
