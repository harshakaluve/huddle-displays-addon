# Huddle room displays — LWYD

> **Adapted for the real 6 rooms — 2026-09-16.** `rooms.csv` / `options.example.json` /
> `esphome/panel-01..06.yaml` originally shipped with 10 sample rooms
> (boardroom, huddle-1..3, studio, edit-1/2, pitch, quiet-1/2) — replaced with
> Huddle's actual 6 Graph-discovered rooms (Main Conference, Partners Mini
> Conference, CS Conference, Designers Conference, Pod 1, Pod 2; real
> mailboxes + capacities pulled live from `GET /api/kiosk/status`). Two things
> worth knowing before you deploy:
> 1. **`graph_tenant_id`/`graph_client_id`/`graph_client_secret`** can reuse
>    the exact values already in `room-dashboard/server/.env` — that Entra
>    app registration already has app-only Graph calendar access. No new App
>    registration/admin-consent needed unless you'd rather keep this add-on's
>    credentials separate from Huddle's own server.
> 2. **With `organiser_source: graph` (the shipped default), step 1 in the
>    guide below — installing HACS + MS365-Calendar + adding `calendar.room_*`
>    entities to HA — can be skipped entirely.** `sources.py`'s `GraphEnricher`
>    talks to Microsoft Graph directly and is the only path used whenever
>    Graph credentials are set; HA's own calendar integration is only consulted
>    as a fallback when `organiser_source: ha` or `none`.
>
> Everything below this line is Raghav's original guide, unedited. Full local
> verification of this adapted config: `python3 test_e2e.py` (ALL PASS),
> `python3 preview.py`, and a manual render using the real room names/mailboxes
> above (including the longest name, "Partners Mini Conference", to check the
> header's auto-sizing) — all clean, no layout issues. Not yet tested: real
> HA/Graph connectivity or actual e-paper hardware.


Ten XIAO 7.5" ePaper panels, one per meeting room, showing what's booked from
Microsoft 365 via Home Assistant. Deep sleep, refreshing on the clock at
**:14 / :29 / :44 / :59** on weekdays, asleep over the weekend while they charge.

**This file is the whole deployment.** Work through it top to bottom.

```
huddle-displays/
├── README.md                 ← you are here; the deploy guide
├── rooms.csv                 ← EDIT THIS FIRST: your ten rooms
├── options.example.json      ← generated add-on config, ready to paste
├── PANEL-LABELS.txt          ← print, cut, stick on the back of each unit
│
├── config.yaml               add-on manifest + options schema
├── Dockerfile  run.sh  requirements.txt
├── app/                      the renderer service
│   ├── main.py               /plan  /image  /preview  /panels  /stay-awake  /health
│   ├── render.py             the 800×480 1-bit layout
│   ├── sleeplan.py           boundary alignment + weekend chunking
│   ├── sources.py            HA calendar REST + optional Graph enrichment
│   └── model.py  config.py  theme.py
├── esphome/
│   ├── huddle-base.yaml      shared package — all the logic lives here
│   └── panel-01.yaml … panel-10.yaml
├── tools/
│   ├── make_rooms.py         rooms.csv → 10 panel files + config + labels
│   └── battery.py            runtime model
├── site/                     source for the reference page
├── out/                      rendered sample screens
├── preview.py                render samples with no HA
└── test_e2e.py               fake HA → service → 1-bit image, end to end
```

---

## Deploy

### 0 · Exchange — do this before anything else

**This is the step that silently ruins room displays.** Exchange creates room
mailboxes with `DeleteSubject $true` and `AddOrganizerToSubject $true`, so the
room's copy of every booking has its **subject thrown away and replaced with the
organiser's name**. Your panel would show "Sanjana Taneja" where the meeting
title belongs, and no code fixes it — the real subject was never written to the
room calendar.

```powershell
Connect-ExchangeOnline

"boardroom","huddle1","huddle2","huddle3","studio",
"edit1","edit2","pitch","quiet1","quiet2" | ForEach-Object {
  Set-CalendarProcessing -Identity "$_@lwyd.in" `
    -DeleteSubject $false `
    -AddOrganizerToSubject $false `
    -RemovePrivateProperty $false `
    -AddAdditionalResponse $false
}
```

`RemovePrivateProperty $false` preserves the **Private** flag that the redaction
keys off. Without it every booking arrives as `normal` sensitivity and nothing
is ever hidden.

⚠️ Applies to **future** bookings only — Exchange will not backfill existing
meetings. Run it, then let a day of bookings accumulate before judging output.

### 1 · Calendars into Home Assistant

Install **MS365-Calendar** from HACS, add the ten room mailboxes, and confirm
you get `calendar.room_boardroom` and friends. `rooms.csv` assumes that naming;
if yours differ, fix `calendar_entity` in the options.

### 2 · Decide how you get the organiser

HA's calendar API returns summary / start / end / description and **nothing
else** — no organiser, no sensitivity. Pick one:

| `organiser_source` | How | Setup |
|---|---|---|
| `graph` *(recommended)* | Add-on calls Graph app-only, gets `organizer` and `sensitivity` as real fields | App registration, `Calendars.Read` **application** permission, admin consent |
| `ha` | Leave `AddOrganizerToSubject $true` in step 0 and parse the organiser out of the subject | Nothing, but the format varies by tenant — verify against a real booking |
| `none` | Subject and times only | Nothing |

For `graph`: Entra ID → App registrations → New → API permissions → Microsoft
Graph → **Application** → `Calendars.Read` → Grant admin consent. Scope it to
the ten room mailboxes with an [application access
policy](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access), or
the registration can read every calendar in the tenant.

### 3 · Edit `rooms.csv`, generate everything

```csv
panel_id,room_id,name,mailbox,static_ip,capacity
panel-01,boardroom,Boardroom,boardroom@lwyd.in,10.0.0.101,12
```

`panel_id` is the **physical unit**, `room_id` is the **door**. Keeping them
separate is what makes step 8 survivable.

```bash
python3 tools/make_rooms.py rooms.csv
```

Writes `esphome/panel-NN.yaml` ×10, prints the `rooms:` block for the add-on,
and writes `PANEL-LABELS.txt`. Set your renderer IP and gateway in the `NET`
dict at the top of that script first.

### 4 · Install the add-on

```bash
mkdir -p /addons/huddle_displays
# copy config.yaml, Dockerfile, run.sh, requirements.txt and app/ into it
```

Settings → Add-ons → Add-on Store → ⋮ → Check for updates → **Local add-ons** →
Huddle Room Displays → Install.

Paste the `rooms:` block (or all of `options.example.json`) into Configuration,
fill in the Graph credentials if you chose `graph`, and Start.

Drop your logo at `/share/huddle/logo.png` — any size, mono or colour; it's
scaled to 26 px tall and thresholded. Without it the header sets a **LWYD**
wordmark.

### 5 · Look at it before you touch hardware

```
http://homeassistant.local:8099/preview/panel-01
```

Live screen in a browser, reloading every 15 s. Iterate on `app/render.py`
here — restarting the add-on is a two-second loop, reflashing ten panels is not.

```
/panels    which panel is on which door
/health    per-room errors, stay-awake state
```

### 6 · Flash

`secrets.yaml` in your ESPHome config dir:

```yaml
wifi_ssid: "LWYD-IoT"
wifi_password: "..."
ota_password: "..."
```

Put `huddle-base.yaml` beside the per-panel files. Flash `panel-01.yaml` over
USB. Everything after that is OTA.

### 7 · Label and hang

Print `PANEL-LABELS.txt`, stick one on the back of each unit. Hang them.

### 8 · The weekend charging round

Support takes all ten down on Friday, docks them, re-hangs them Monday. Two
things make that safe:

- **The room name is the second-largest thing on the screen**, so a panel on the
  wrong door is obvious the moment anyone walks past.
- **Firmware carries the panel id, not the room.** If panel-03 comes back on
  Huddle 7's door, change one `panel_id` line in the add-on options and restart
  it. No reflash, no relabelling, effective on the next cycle.

Tell support: **if a screen looks wrong or stale, press the reset button on the
back.** It forces an immediate wake, fetch and redraw. That single instruction
resolves most of what they'll ever report.

Panels sleeping on the dock still charge — the charge circuit is independent of
the MCU. They wake on schedule all Monday morning, so a panel re-hung at 09:00
is showing data at most 15 minutes old.

---

## Battery

Not a constraint on a weekly recharge, and the numbers say so with room to
spare. `python3 tools/battery.py` re-runs this with your own measurements.

One full cycle costs about **128 µAh** (2 s wifi assoc, 0.8 s for the plan and
the 48 KB image, 4.5 s e-paper refresh). Against a five-working-day budget:

| Refresh | Cycles/day | Runtime @ 800 µA sleep (worst case) | Fits 5 days? |
|---|---|---|---|
| **15 min** *(shipped)* | 96 | ~57 days | 11× margin |
| 5 min | 288 | ~32 days | 6.4× margin |
| 2 min | 720 | ~16 days | 3.2× margin |
| 1 min | 1440 | ~9 days | 1.8× margin |

At the shipped 15-minute cadence you're using about **5% of the cell each week**
— so shallow it'll barely age the battery.

**A 5-minute refresh is comfortably within budget** and is the single biggest
felt improvement: a room booked two minutes ago shows up before someone walks to
it. It's one line:

```yaml
refresh_minutes: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
```

No reflash — the panels ask the server for their schedule. The only cost is that
each panel flashes through its refresh cycle twelve times an hour instead of
four, which is briefly visible from a corridor. I've left 15 minutes as shipped
because that's what you specified; try 5 for a week and see whether the flicker
bothers anyone.

### Fuel gauge: now optional

The panel ships with no battery monitoring, and on the ESP32-C3 you can't add it
with a resistor divider — ADC1 is GPIO0–GPIO4 and this panel already uses GPIO2
(reset), GPIO3 (CS) and GPIO4 (busy); ADC2 is unusable while wifi is on. The
only route is an I²C fuel gauge (MAX17048) on GPIO6/GPIO7, and the block is in
`huddle-base.yaml`, commented out.

**On a fixed weekly charge you don't need it** — you're never guessing at
charge state. Fit it only if you later want to catch a cell that's aged out.

Cheaper insurance, no soldering: alert on silence. Any panel that hasn't hit
`/plan` for 45 minutes on a weekday is flat, crashed or off the network, and the
add-on's telemetry push makes that a one-line HA automation.

---

## Timing

The panel wakes `lead_seconds` (default 25) **before** the boundary, so by the
time the ~4.5 s refresh finishes the clock reads :14 / :29 / :44 / :59. The
"Updated HH:MM" stamp is rendered server-side at request time, so it always
tells the truth about the image in front of you.

**The panel has no clock and never runs SNTP.** Every wake it asks the add-on
how long to sleep. Alignment is therefore recomputed server-side every cycle, so
ESP32 RTC drift (1–2%) never accumulates, and the whole schedule changes in one
add-on option instead of ten flashes.

**Weekend sleep is chunked on purpose.** One 60-hour deep sleep would drift by
up to an hour. Instead the panel takes six-hour naps and is told
`render: false` for all but the last — it wakes, asks, and goes straight back to
sleep without touching the e-paper, which is the expensive part of a cycle. The
final nap lands it at Monday 00:13:35.

---

## Screen states

| State | When | Shows |
|---|---|---|
| **In use** | Meeting on now | Title, organiser, times, duration, elapsed bar, "Next …" |
| **Private** | Organiser marked it Private/Confidential | "Private meeting", organiser hidden, times still shown |
| **Available** | Nothing booked now | Big AVAILABLE, free-until, QR to book |
| **No data** | Calendar unreachable **and** nothing cached | "NO DATA — can't reach the calendar" |

That last one matters more than it looks. With no events in hand the free-state
logic would render a confident **AVAILABLE**, and someone walks into a room
that's actually booked. An unreachable calendar is not an empty one. If the
add-on has a cached copy it shows that instead, with `Stale — last sync HH:MM`
in the header.

Samples for all four are in `out/`, rendered by the production renderer.

---

## Layout

800 × 480, one ink colour. 76 px inverted header, 292 px body, 112 px timeline.

Two deliberate calls in the timeline:

- **No tint on elapsed time.** Any dither dense enough to see from across a
  corridor is also dense enough to be mistaken for a booked block. White = free,
  black = booked, marker = now. (The first version shaded the past at 25% and
  was genuinely unreadable.)
- **Hard threshold, not Floyd–Steinberg.** Dithered type on e-paper looks like
  dirt. Text is antialiased in 8-bit then cut at 150 — slightly fat, very crisp.

Set `day_start` / `day_end` to when your rooms actually get booked; the axis is
proportional to that window, so 08:00–20:00 wastes no pixels on hours nobody
books.

---

## Why BMP, not PNG

The ESP32-C3 has no PSRAM. At draw time the display buffer (48 KB) and the
decoded image (48 KB) are both resident. PNG adds ~40 KB of inflate window; BMP
needs almost nothing, and 48 KB over LAN is under a second.

If you hit heap trouble anyway, the panel takes any XIAO module — a **XIAO
ESP32-S3** drops into the socket with 8 MB PSRAM and the problem disappears
(change `board:` and `variant:`). Or set `image_format: png` to trade RAM for
radio time.

---

## OTA on a sleeping panel

```bash
curl -X POST http://homeassistant.local:8099/stay-awake/panel-01?on=1
# at its next wake (≤15 min) it calls deep_sleep.prevent
#   ... flash it ...
curl -X POST http://homeassistant.local:8099/stay-awake/panel-01?on=0
```

`run_duration: 150s` means a wedged cycle sleeps 15 minutes rather than
flattening the battery awake — but it also caps the OTA window, so release and
re-hold if you need longer.

---

## Tests

```bash
python3 test_e2e.py     # fake HA → service → 1-bit image; run after any render.py change
python3 preview.py      # sample screens into out/, no HA needed
python3 tools/battery.py
```

---

## Still open

- **Time a real refresh** and tune `refresh_settle` (default 8 s).
- **Try `refresh_minutes` at 5 minutes** for a week — you have the budget.
- **Check `booking_url_template` resolves** for your tenant, or point the QR
  wherever you want.
- **Confirm the Graph registration is scoped** to the ten room mailboxes.
