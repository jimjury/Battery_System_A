"""Reuse the installed IDF and Python. Never installs tools or changes network sharing."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from deploy_web import Device, deploy, load_key
from web_bundle import build as bundle_build
from release import usb_envelope, frame

ROOT = Path(__file__).resolve().parents[1]
IDF = Path('F:/Espressif/esp-idf/esp-idf-v5.5.6')
TOOLS = Path('F:/Espressif/tools')
PYTHON = TOOLS / 'python_env/idf5.5_py3.11_env/Scripts/python.exe'
LOCAL = ROOT / '.local'
PARTITIONS = '''# Name,Type,SubType,Offset,Size,Flags
nvs,data,nvs,0x9000,0x6000,
phy_init,data,phy,0xf000,0x1000,
ota_0,app,ota_0,0x10000,0x400000,
ota_1,app,ota_1,0x410000,0x400000,
otadata,data,ota,0x810000,0x2000,
web_a,data,spiffs,0x820000,0x100000,
web_b,data,spiffs,0x920000,0x100000,
'''


def env():
    value = os.environ.copy()
    value.update(IDF_PATH=str(IDF), IDF_TOOLS_PATH=str(TOOLS),
                 IDF_PYTHON_ENV_PATH=str(PYTHON.parent.parent), IDF_TARGET='esp32s3',
                 PYTHONUTF8='1')
    executables = list((TOOLS / 'tools').rglob('cmake.exe')) + list((TOOLS / 'tools').rglob('ninja.exe')) + list((TOOLS / 'tools').rglob('xtensa-esp32s3-elf-gcc.exe'))
    directories = [str(PYTHON.parent)] + sorted({str(p.parent) for p in executables})
    value['PATH'] = os.pathsep.join(directories + [value.get('PATH', '')])
    rom = sorted((TOOLS / 'tools/esp-rom-elfs').rglob('esp32s3_rev0_rom.elf'))
    if rom:
        value['ESP_ROM_ELF_DIR'] = str(rom[-1].parent)
    return value


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], cwd=ROOT, env=env(), check=True, **kwargs)


def idf(*args):
    run([PYTHON, IDF / 'tools/idf.py', *args])


def setup():
    LOCAL.mkdir(exist_ok=True)
    key_file = LOCAL / 'update_key.txt'
    if not key_file.exists():
        with key_file.open('x', encoding='ascii') as f:
            f.write(secrets.token_hex(32) + '\n')
    key = load_key()
    if key != key_file.read_text(encoding='ascii').strip():
        raise RuntimeError('Environment key differs from local provisioned key; refusing implicit key rotation')
    admin = LOCAL / 'admin_password.txt'
    if not admin.exists():
        with admin.open('x', encoding='ascii') as file: file.write(secrets.token_hex(16) + '\n')
    print('Device credentials remain local; provisioning stores them in NVS. Public firmware contains no device secret.')


def provision(device):
    status = json.loads(device.request('/api/session'))
    if status.get('provisioned'):
        print('Device already provisioned; existing NVS credentials retained.'); return
    password = (LOCAL / 'admin_password.txt').read_text(encoding='ascii').strip()
    data = json.dumps({'update_key': load_key(), 'admin_hash': hashlib.sha256(password.encode()).hexdigest()}).encode()
    response = json.loads(device.request('/api/provision', data, {'Content-Type': 'application/json'}))
    if not response.get('provisioned'): raise RuntimeError('Device provisioning did not persist')
    print('Provisioning complete. Administrator password: .local/admin_password.txt (not printed).')


def review_build():
    """Compile the proposal in an isolated build; never activate its partition table."""
    LOCAL.mkdir(exist_ok=True)
    table = LOCAL / 'partitions.review.csv'; table.write_text(PARTITIONS, encoding='ascii')
    source = (ROOT / 'sdkconfig').read_text(encoding='utf-8')
    changes = {'CONFIG_PARTITION_TABLE_SINGLE_APP': 'n', 'CONFIG_PARTITION_TABLE_CUSTOM': 'y',
        'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME': '"' + table.as_posix() + '"',
        'CONFIG_PARTITION_TABLE_FILENAME': '"' + table.as_posix() + '"',
        'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE': 'y', 'CONFIG_SPIFFS_OBJ_NAME_LEN': '96',
        'CONFIG_LWIP_IP_FORWARD': 'n', 'CONFIG_LWIP_IPV4_NAPT': 'n'}
    lines = [line for line in source.splitlines() if not any(line.startswith(k+'=') or line == '# '+k+' is not set' for k in changes)]
    lines += [f'{k}={v}' if v != 'n' else f'# {k} is not set' for k, v in changes.items()]
    config = LOCAL / 'sdkconfig.review'; config.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    idf('-B', 'build-review', '-D', 'SDKCONFIG='+config.as_posix(), 'build')


def digest(file):
    return hashlib.sha256(Path(file).read_bytes()).hexdigest()


def backup_flash(port):
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    directory = ROOT.parent / 'Battery_System_A_backups' / ('flash_' + stamp)
    directory.mkdir(parents=True)
    first, second = directory / 'flash.bin', directory / 'flash.verify.bin'
    # Two independent reads detect serial transfer corruption. Resets the device.
    for output in [first, second]:
        run([PYTHON, '-m', 'esptool', '--chip', 'esp32s3', '--port', port,
             '--baud', '921600', 'read_flash', '0', '0x1000000', output])
        if output.stat().st_size != 0x1000000:
            raise RuntimeError('Incomplete flash backup')
    if digest(first) != digest(second):
        raise RuntimeError('Flash backup reads disagree. No flashing permitted.')
    report = {'flash': str(first), 'sha256': digest(first), 'bytes': first.stat().st_size,
              'verified_two_reads': True, 'port': port}
    (directory / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return directory


def configure_migration():
    (ROOT / 'partitions.csv').write_text(PARTITIONS, encoding='ascii')
    config = ROOT / 'sdkconfig'
    lines = config.read_text(encoding='utf-8').splitlines() if config.exists() else []
    changes = {
        'CONFIG_PARTITION_TABLE_SINGLE_APP': 'n',
        'CONFIG_PARTITION_TABLE_CUSTOM': 'y',
        'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME': '"partitions.csv"',
        'CONFIG_PARTITION_TABLE_FILENAME': '"partitions.csv"',
        'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE': 'y',
        'CONFIG_ESPTOOLPY_FLASHSIZE_16MB': 'y',
        'CONFIG_ESPTOOLPY_FLASHSIZE': '"16MB"',
        'CONFIG_LWIP_IP_FORWARD': 'n',
        'CONFIG_LWIP_IPV4_NAPT': 'n',
        'CONFIG_SPIFFS_OBJ_NAME_LEN': '96',
    }
    def belongs(line):
        return any(line.startswith(k + '=') or line == '# ' + k + ' is not set' for k in changes)
    lines = [line for line in lines if not belongs(line)]
    lines += [f'{key}={value}' if value != 'n' else f'# {key} is not set' for key, value in changes.items()]
    config.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    # Safe, reproducible defaults with no credentials.
    defaults = ROOT / 'sdkconfig.defaults'
    defaults.write_text('CONFIG_TINYUSB_NET_MODE_NCM=y\n' + '\n'.join(
        f'{k}={v}' if v != 'n' else f'# {k} is not set' for k, v in changes.items()) + '\n', encoding='ascii')


def require_migrated():
    text = (ROOT / 'sdkconfig').read_text(encoding='utf-8')
    if 'CONFIG_PARTITION_TABLE_CUSTOM=y' not in text or 'CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y' not in text:
        raise RuntimeError('Approved partition migration must be completed first')
    if (ROOT / 'partitions.csv').read_text(encoding='ascii') != PARTITIONS:
        raise RuntimeError('Partition layout differs from reviewed migration')


def wait_device(device, expected=None):
    deadline = time.monotonic() + 100
    last = None
    while time.monotonic() < deadline:
        try:
            status = device.status()
            if not status.get('storage_ready'):
                raise RuntimeError('Device is missing web storage')
            if expected and status.get('firmware_sha256') != expected:
                raise RuntimeError('Expected firmware identity has not booted')
            if not device.request('/'):
                raise RuntimeError('HTTP self-test returned an empty page')
            return status
        except (OSError, ValueError, RuntimeError) as exc:
            last = exc; time.sleep(1)
    raise RuntimeError(f'Device health check timed out: {last}')


def flash(port):
    require_migrated()
    approved = json.loads((LOCAL / 'approved_partition.json').read_text())
    if digest(ROOT / 'build/partition_table/partition-table.bin') != approved['partition_sha256']:
        raise RuntimeError('Partition table changed; a new reviewed migration is required')
    idf('-p', port, 'flash')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'build', 'flash', 'backup-flash', 'migrate', 'ota', 'provision'])
    parser.add_argument('--port', default='COM12')
    parser.add_argument('--url', default='http://192.168.7.1')
    parser.add_argument('--approved', action='store_true', help='Operator has approved the documented first migration')
    args = parser.parse_args()
    if args.action == 'setup': setup(); return
    if args.action == 'backup-flash': backup_flash(args.port); return
    setup()
    device = Device(args.url, load_key())
    if args.action == 'provision': provision(device); return
    if args.action == 'migrate':
        if not args.approved:
            raise RuntimeError('Review docs/MIGRATION.md and approve before running migrate --approved')
        # No partition or sdkconfig mutation until the full physical backup passes.
        backup = backup_flash(args.port)
        configure_migration()
        idf('build')
        record = {'partition_sha256': digest(ROOT / 'build/partition_table/partition-table.bin'),
                  'flash_backup': str(backup), 'time': time.time()}
        (LOCAL / 'approved_partition.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        flash(args.port)
        wait_device(device)
        provision(device)
        bundle, files = bundle_build(ROOT / 'web'); deploy(device, bundle, files)
        (LOCAL / 'migration_complete.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        print('Migration completed; USB web deployment verified.'); return
    if args.action in ('ota', 'flash'): require_migrated()
    if args.action == 'build' and not (ROOT / 'partitions.csv').exists():
        review_build(); return
    idf('build')
    if args.action == 'flash':
        flash(args.port); wait_device(device)
    if args.action == 'ota':
        before = device.status()
        if before.get('pending') or before.get('firmware_pending'):
            raise RuntimeError('Confirm or roll back the pending update first')
        description = json.loads((ROOT / 'build/project_description.json').read_text())
        image = ROOT / 'build' / description['app_bin']
        data = image.read_bytes(); expected = hashlib.sha256(data).hexdigest()
        try:
            envelope = usb_envelope('firmware', data, before.get('firmware_sequence_floor', 0))
            device.post('/api/firmware', frame(envelope, data, 'firmware'))
        except (OSError, ValueError):
            # Reboot can race the final HTTP reply. Verify identity before accepting.
            pass
        wait_device(device, expected)
        device.post('/api/firmware/confirm')
        if device.status().get('firmware_pending'):
            raise RuntimeError('Firmware confirmation failed')
        print('OTA firmware booted, identity verified, and confirmed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr); sys.exit(1)
