import argparse
from pathlib import Path
import threading
import webbrowser
from ab_platform.http_service import configure_log,serve
from .service import BService


def main():
    parser=argparse.ArgumentParser(description='Portable, domain-neutral B: independent local inference plus outbound C service')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent/'.runtime')
    parser.add_argument('--port',type=int,default=8765);parser.add_argument('--open',action='store_true')
    args=parser.parse_args();configure_log(args.root);service=BService(args.root);server=serve(service,'127.0.0.1',args.port)
    url=f'http://127.0.0.1:{server.server_port}';print(url,flush=True);print('Administrator credential remains in the local data root.',flush=True)
    if args.open:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();service.close()


if __name__=='__main__':main()
