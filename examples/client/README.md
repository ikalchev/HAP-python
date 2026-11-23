# Client examples

## Driving an accessory with a HAP client (`pyhap.client`)

`lightbulb_accessory.py` runs a small Lightbulb accessory;
`control_lightbulb.py` toggles its `On` characteristic over the real HAP
protocol (pair-verify, encrypted transport) using a persistent controller
identity. See the [pyhap.client API docs](../../docs/source/api/client.rst)
for the pairing and trust details.

Same machine (paths below are relative to this directory; the state files
always land next to the scripts):

```sh
cd examples/client
python -m pyhap.client new -f client.state   # once; prints client.json
python lightbulb_accessory.py                # pairs client.state automatically
python control_lightbulb.py                  # toggles the light
```

Client on a different machine than the accessory: on the client machine run
`python -m pyhap.client new -f client.state` and copy only the printed JSON
(`client.json`, uuid + public key) to the accessory machine. With the
accessory stopped, run there:

```sh
python -m pyhap.client register -f client.json -a accessory.state
```

then start the accessory again and run `control_lightbulb.py` on the client
machine (adjust `HOST`/`PORT` in the script). Use
`python -m pyhap.client list -a accessory.state` and
`python -m pyhap.client deregister -a accessory.state <uuid> --yes` to manage
pairings on the accessory machine.

The `*.state` files created here are gitignored.
