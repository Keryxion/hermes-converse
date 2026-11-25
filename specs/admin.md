# Admin spec — how to manage Arya devices

`hermes-converse` exposes one WebSocket route for managing the devices attached
to it:

```
GET /v1/audio/admin
```

It is for software that looks after the devices, such as the Arya Devices
integration for Home Assistant, and not for a voice device; those use
[the client spec](client.md). An admin connection sees every device, is told
when one changes, and can announce on devices and change their settings.

## 1. Connect

Send the API key on the WebSocket request:

```
Authorization: Bearer <API_SERVER_KEY>
```

A missing or wrong key is refused with HTTP 401 and no WebSocket. All frames are
JSON text frames with a `type`. Ignore a frame whose `type` you do not know, and
fields you do not know. Any number of admin connections can be open; each is
sent every change.

## 2. Devices

The first frame lists every device the server knows. It forgets one that has
not been attached for 30 days.

```json
{"type": "devices", "devices": [
  {"device_name": "Kitchen", "attached": true, "muted": false, "volume": 40, "address": "192.168.1.52"},
  {"device_name": "Den Echo", "attached": false, "muted": null, "volume": null, "address": "192.168.1.60"}]}
```

| field | what it is |
|---|---|
| `device_name` | The device's name, which identifies it in every frame. |
| `attached` | Whether the device has a voice connection now. |
| `muted` | Whether its microphone is muted. `null` while it is not attached, or when it has not said. |
| `volume` | Its playback volume, an integer from 0 to 100. `null` while it is not attached, or for a device with no volume to set. |
| `address` | The IP address it last connected from, kept while it is away so the device can be found on the network. `null` when none is known. |

Whenever anything about a device changes, or a new device connects for the first
time, the server sends that device whole:

```json
{"type": "device", "device_name": "Kitchen", "attached": true, "muted": true, "volume": 40, "address": "192.168.1.52"}
```

When the server forgets a device, it says so:

```json
{"type": "device_removed", "device_name": "Den Echo"}
```

After a reconnect, the new `devices` frame replaces everything known before:
forget a device it does not name.

## 3. Requests

A request carries a `request_id`, any string of your choosing. The server
answers each with one `result` carrying the same `request_id`. Results can
arrive in any order and between `device` frames.

A request that fails is answered with an `error`:

```json
{"type": "result", "request_id": "7", "error": "not_attached"}
```

| `error` | Meaning |
|---|---|
| `bad_request` | A field is missing or has the wrong type or range, or the `type` is not a request. |
| `unknown_device` | No device has that name. |
| `not_attached` | The device is not attached. Nothing is kept for later. |

A frame that is not a JSON object, or has no string `request_id`, is ignored.

### Announce

Speak a message on devices, after a chime and with no agent turn:

```json
{"type": "announce", "request_id": "7", "message": "The laundry is done.", "target": ["Kitchen", "Den Echo"]}
```

| field | what it does |
|---|---|
| `message` | The text to speak. Required; a non-empty string after trimming. |
| `target` | A `device_name`, or an array of them. Omitted, it is every attached device. |

```json
{"type": "result", "request_id": "7", "announced": ["Kitchen"], "skipped": ["Den Echo"]}
```

`announced` lists the devices that accepted the announcement, and `skipped` the
named devices that are not attached or are unknown. A device that is in an
exchange accepts it and plays it afterwards. Nothing is kept for a device that
is skipped.

### Set

Change a device's microphone mute, its volume, or both:

```json
{"type": "set", "request_id": "8", "device_name": "Kitchen", "muted": true, "volume": 40}
```

`muted` is a Boolean and `volume` an integer from 0 to 100; send at least one.
The result holds the device as it then is:

```json
{"type": "result", "request_id": "8",
 "device": {"device_name": "Kitchen", "attached": true, "muted": true, "volume": 40, "address": "192.168.1.52"}}
```

The device decides. One that cannot apply a change, such as unmuting past a
hardware switch, is shown unchanged, and so is one that did not answer within
2 s. A change the device does make also arrives as a `device` frame, on this and
every other admin connection.
