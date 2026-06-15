# OceanStor Simulator

Dummy Huawei OceanStor REST API simulator for testing `charm-cinder-oceanstor` without a real storage device.

## Quick Start

```bash
docker compose up -d
```

The simulator listens on `https://localhost:8088` with a self-signed certificate.

## Configure the Charm

Set the charm's `resturl` to point at this simulator:

```bash
juju config cinder-oceanstor \
  resturl=https://<docker-host-ip>:8088/deviceManager/rest/ \
  username=admin \
  userpassword=Admin@123 \
  storagepool=OpenStack_Pool \
  protocol=iSCSI \
  iscsidefaulttargetip=192.168.1.100
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OCEANSTOR_POOL` | `OpenStack_Pool` | Storage pool name(s), semicolon-separated |
| `OCEANSTOR_TARGET_IP` | `192.168.1.100` | IP returned as iSCSI target |

## Verify

```bash
# Login
curl -k -X POST https://localhost:8088/deviceManager/rest/xx/sessions \
  -d '{"username":"admin","password":"Admin@123","scope":"0"}'

# List pools
curl -k -H "iBaseToken: simulator-token-001" \
  https://localhost:8088/deviceManager/rest/210235G7J20000000000/storagepool
```

## Run Without Docker

```bash
python3 oceanstor_simulator.py --port 8088 --pool OpenStack_Pool --cert-dir /tmp/certs
```

Use `--no-ssl` for plain HTTP (debugging only).
