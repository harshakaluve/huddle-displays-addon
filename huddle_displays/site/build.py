"""Inline the rendered mockups into the reference page as data: URIs."""
import base64
import pathlib

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent / "out"

SCREENS = [
    ("01-busy.png", "In use", "A meeting is on now",
     "Title, organiser, start and end, duration, an elapsed bar, and what's booked next. "
     "The room name sits second-loudest after the status word, because people in a corridor "
     "are looking for a room, not a meeting."),
    ("03-private.png", "Private", "Organiser marked it Private",
     "Subject collapses to &ldquo;Private meeting&rdquo; and the name disappears, but the times "
     "stay &mdash; the room still reads as occupied. Staff opt out using a flag they already "
     "know how to set in Outlook."),
    ("02-free.png", "Available", "Nothing booked right now",
     "Free-until time, the gap in words, and a QR that opens the room's booking page on a phone. "
     "The QR only appears on screens where booking is the next action."),
    ("e2e-ha-down.png", "No data", "Calendar unreachable, nothing cached",
     "This state matters more than it looks. With no events in hand the free-state logic would "
     "render a confident AVAILABLE and somebody walks into a booked room. An unreachable calendar "
     "is not an empty one, and the screen has to say so."),
]

TPL = """    <figure class="screen">
      <div class="bezel"><img src="data:image/png;base64,{b64}" width="800" height="480"
        alt="{alt}"></div>
      <figcaption class="cap">
        <h3>{name}</h3>
        <span class="when">{when}</span>
        <p>{body}</p>
      </figcaption>
    </figure>"""


def main() -> None:
    blocks = []
    for filename, name, when, body in SCREENS:
        b64 = base64.b64encode((OUT / filename).read_bytes()).decode()
        blocks.append(TPL.format(b64=b64, name=name, when=when, body=body,
                                 alt=f"{name} state: {when}"))
    html = (HERE / "template.html").read_text().replace("{{SCREENS}}", "\n".join(blocks))
    dest = HERE / "index.html"
    dest.write_text(html)
    print(f"{dest}  {len(html)/1024:.0f} KB")


if __name__ == "__main__":
    main()
