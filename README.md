# mqtty

`mqtty` bridges MQTT topics to either your local terminal or a pseudoterminal (PTY), letting you interact with serial-style streams over MQTT.

This is useful for debugging, automation, or tunneling serial communication through a networked MQTT broker.

## Usage

```bash
mqtty mqtt://<host>/<topic> [--pts-only]
```

By default, `mqtty` runs in bidirectional terminal mode:

* bytes from `stdin` are published to `.../device_serial_input`
* bytes from `.../device_serial_output` are written to `stdout`
* if `stdin` is a TTY, it is placed into raw mode while `mqtty` is running
* type `Ctrl-A Ctrl-X` to exit, matching picocom's default exit sequence
* type `Ctrl-A Ctrl-A` to send a literal `Ctrl-A`

Use `--pts-only` if you only want a PTY device:

* `--pts-only`, `-p`:
  Print the path to the PTS device and keep it open without attaching local stdin/stdout. Useful for tools or scripts that want to use the virtual serial port.

The `<topic>` segment of the URI is used as the base path for MQTT messages:

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

## License

MIT
