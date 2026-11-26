import argparse
from pathlib import Path
from ab_platform.http_service import configure_log,serve
from .api import CenterAPI


def main():
    parser=argparse.ArgumentParser(description='Independent C; production requires its own authorized cloud host and TLS')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent/'.runtime')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--cert');parser.add_argument('--key');parser.add_argument('--origin')
    args=parser.parse_args();configure_log(args.root);service=CenterAPI(args.root)
    server=serve(service,args.host,args.port,cert=args.cert,key=args.key,origin=args.origin)
    print(f'C listening on {args.host}:{server.server_port}; local development is not cloud acceptance.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();service.close()


if __name__=='__main__':main()
