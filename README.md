# mqtty

`mqtty` bridges MQTT topics to either your local terminal or a pseudoterminal (PTY), and now also ships log capture and replay helpers for the same serial stream format.

This is useful for debugging, automation, or tunneling serial communication through a networked MQTT broker.

## Commands

```bash
mqtty mqtt://<host>/<topic> [--pts-only]
```

`mqtty` runs in bidirectional terminal mode by default:

* bytes from `stdin` are published to `.../device_serial_input`
* bytes from `.../device_serial_output` are written to `stdout`
* if `stdin` is a TTY, it is placed into raw mode while `mqtty` is running
* type `Ctrl-A Ctrl-X` to exit, matching picocom's default exit sequence
* type `Ctrl-A Ctrl-A` to send a literal `Ctrl-A`

Use `--pts-only` if you only want a PTY device:

* `--pts-only`, `-p`:
  Print the path to the PTS device and keep it open without attaching local stdin/stdout. Useful for tools or scripts that want to use the virtual serial port.

`mqtty-log` records `.../device_serial_output` into replay files:

```bash
mqtty-log device mqtt://<host>/<serial-server>/<device> capture.jsonl.zst
mqtty-log prefix mqtt://<host>/<topic-prefix> logs/
```

* `device` records one full device URL into a single compressed replay file and adds `.zst` if omitted
* `prefix` subscribes to `<topic-prefix>/#`, keeps topics ending in `device_serial_output`, and rotates per-device logs under `<outdir>/<topic-path-below-prefix>/YYYY-MM-DD_replay.jsonl.zst`

`mqtty-log-replay` replays either plain or compressed replay logs:

```bash
mqtty-log-replay path/to/replay.jsonl.zst [--raw] [--follow] [--info]
```

Raw `.jsonl` logs are still accepted for replay and inspection.

`mqtty-serial-bridge` discovers local serial devices and bridges them to MQTT:

```bash
mqtty-serial-bridge [--config /etc/mqtty-serial-bridge.toml]
```

It subscribes to:

* `<topic_base>/<port>/device_serial_input`

And publishes:

* `<topic_base>/<port>/device_serial_output`

Default config lookup order:

1. `/etc/mqtty-serial-bridge.toml`
2. `/etc/uart2mqtt.toml` (legacy fallback)

The `<topic>` segment of the URI is used as the base path for MQTT messages across both live commands:

* `.../device_serial_input` receives bytes from local input and sends them to the device
* `.../device_serial_output` carries device output back to your terminal or PTY

## Example

```bash
mqtty mqtt://broker.local/mydevice
```

This connects your terminal directly to `mydevice/device_serial_input` and `mydevice/device_serial_output`.

To expose a PTY instead:

```bash
mqtty mqtt://broker.local/mydevice --pts-only
```

This prints the local PTY path and keeps it bridged to the same MQTT topics until interrupted.

To capture a replay log while a device is running:

```bash
mqtty-log device mqtt://broker.local/mydevice mydevice.jsonl.zst
```

To capture every device under one serial-server or a broader base prefix:

```bash
mqtty-log prefix mqtt://broker.local/testbench/mark-pantry logs/
mqtty-log prefix mqtt://broker.local/testbench logs/
```

To inspect or replay that log later:

```bash
mqtty-log-replay mydevice.jsonl.zst --info
mqtty-log-replay mydevice.jsonl.zst
```

To run the serial bridge:

```bash
mqtty-serial-bridge
```

## License

MIT
