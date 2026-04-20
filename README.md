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
mqtty-log mqtt://<host>/<topic> --outfile capture.jsonl.zst
mqtty-log mqtt://<host>/<topic> --service --outdir logs/
```

* `--outfile` writes a single compressed replay file and adds `.zst` if omitted
* `--service --outdir` rotates per-device files under `<outdir>/<server>/<device>/YYYY-MM-DD_replay.jsonl.zst`

`mqtty-log-replay` replays either plain or compressed replay logs:

```bash
mqtty-log-replay path/to/replay.jsonl.zst [--raw] [--follow] [--info]
```

Raw `.jsonl` logs are still accepted for replay and inspection.

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
mqtty-log mqtt://broker.local/mydevice --outfile mydevice.jsonl.zst
```

To inspect or replay that log later:

```bash
mqtty-log-replay mydevice.jsonl.zst --info
mqtty-log-replay mydevice.jsonl.zst
```

## License

MIT
