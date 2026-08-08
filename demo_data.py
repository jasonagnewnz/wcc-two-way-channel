"""Demo content for the message board.

⚠ EVERY MESSAGE IN THIS FILE IS INVENTED.

The agencies are real — WCC Emergency Management, WREMO, Wellington Water,
FENZ, Police, Greater Wellington, Wellington Free Ambulance, Red Cross — so
that a WCC judge sees their own operating picture rather than a set of made-up
placeholders. None of these bodies wrote any of this, none of it reflects a
real incident, and the interface says so on every agency channel.

If this ever moves beyond a prototype, this file goes and the agency channels
stay empty until the agencies themselves are in them.

Written around one scenario so the board reads as a single event: heavy rain,
surface flooding on Hutt Road, a slip in Wadestown, and a power outage in Aro
Valley — the same incidents as the seeded reports in run.py.
"""

from __future__ import annotations

# (channel_id, author_name, agency, body, visibility)
AGENCY_MESSAGES = [
    ("wcc-em", "Duty Controller", "WCC Emergency Management",
     "Heavy rain warning upgraded to orange for Wellington. Standing up the EOC at reduced staffing from 14:00.", "public"),
    ("wcc-em", "Duty Controller", "WCC Emergency Management",
     "Three community reports of surface flooding on Hutt Road in the last twenty minutes. Grouped as one incident, contractor tasked.", "public"),
    ("wcc-em", "Ops Officer", "WCC Emergency Management",
     "Wadestown slip is footpath-only at this stage. Road remains open. Reassess after the next band of rain.", "public"),
    ("wcc-em", "Ops Officer", "WCC Emergency Management",
     "Holding off on a public evacuation message. Nothing in the reports supports it yet and we don't want to move people unnecessarily.", "officials"),

    ("wremo", "Regional Duty", "WREMO",
     "Aro Valley hub has self-activated on the power outage. Two volunteers on site, they have the sat phone.", "public"),
    ("wremo", "Regional Duty", "WREMO",
     "Reminder to all: hub coordinators are reporting through the community channel, not by ringing the call centre. Please pick reports up from there.", "public"),
    ("wremo", "Regional Duty", "WREMO",
     "Island Bay request for assistance with a mobility-impaired resident has been passed to Free Ambulance.", "public"),

    ("wellington-water", "Network Control", "Wellington Water",
     "Stormwater network at capacity through Ngauranga. Overland flow paths behaving as modelled so far.", "public"),
    ("wellington-water", "Network Control", "Wellington Water",
     "Crew dispatched to the Hutt Road culvert. ETA 25 minutes, traffic management required.", "public"),
    ("wellington-water", "Network Control", "Wellington Water",
     "No wastewater overflows reported at this stage. Monitoring the Aro catchment.", "public"),

    ("fenz", "Comms", "Fire and Emergency NZ",
     "Two appliances committed to pumping out at Ngauranga. No persons reported.", "public"),
    ("fenz", "Comms", "Fire and Emergency NZ",
     "If the Wadestown slip moves onto the carriageway we will need a road closure — flagging early so it isn't a surprise.", "public"),

    ("nz-police", "District Comms", "NZ Police",
     "Units aware of the Hutt Road lane closure. Assisting with traffic management on request.", "public"),
    ("nz-police", "District Comms", "NZ Police",
     "No reports of looting or public order issues. Routine patrols continuing.", "public"),

    ("gwrc", "Flood Protection", "Greater Wellington",
     "Hutt River at Taita Gorge rising but well inside banks. Telemetry updating every 15 minutes.", "public"),
    ("gwrc", "Flood Protection", "Greater Wellington",
     "Metlink advising delays on the Johnsonville line. No suspensions.", "public"),

    ("wfa", "Comms Desk", "Wellington Free Ambulance",
     "Received the Island Bay welfare request. Crew assigned, non-urgent.", "public"),

    ("red-cross", "Welfare Lead", "NZ Red Cross",
     "Two welfare volunteers available if a hub needs relief overnight. Contact through WREMO.", "public"),
]

# (channel_id, author_name, role, body, visibility)
PUBLIC_MESSAGES = [
    ("wellington", "Wellington City Council", "official",
     "Rain is expected to continue until about 8pm. If you see surface flooding, report it here rather than ringing — it reaches the duty officer faster and you'll get an update back.", "public"),
    ("wellington", "Mere", "resident",
     "Is the Ngauranga onramp still passable? Heading north in about an hour.", "public"),
    ("wellington", "Wellington City Council", "official",
     "Southbound lane at Ngauranga is closed, northbound is open with surface water. Take it slowly.", "public"),
    ("wellington", "Tama", "resident",
     "Reminder the Aro Valley hub is open if anyone in that block needs somewhere warm while the power's out.", "public"),

    ("ngauranga", "Priya", "resident",
     "Water's over the kerb outside the container yard now. Was ankle deep twenty minutes ago.", "public"),
    ("ngauranga", "Dave", "resident",
     "Two cars stopped in the flooded bit. Occupants are out and fine, they're waiting on the verge.", "public"),
    ("ngauranga", "Wellington City Council", "official",
     "Thanks both — contractor is en route, about 25 minutes. Please don't drive into it.", "public"),

    ("wadestown", "Ang", "resident",
     "Slip across the footpath near the shops, people are walking on the road to get past. Kids on the school route in the morning is my worry.", "public"),
    ("wadestown", "Wellington City Council", "official",
     "Logged and being checked. If it moves onto the road we'll close it — keep an eye out here.", "public"),

    ("aro-valley", "Aro Valley Community Hub", "hub",
     "Hub is open. Kettle's on, we've got power via the generator and room for about forty.", "public"),
    ("aro-valley", "Sam", "resident",
     "Whole block's been dark since about twenty past. Anyone know an ETA?", "public"),
    ("aro-valley", "Aro Valley Community Hub", "hub",
     "No ETA from the lines company yet. We'll post here the moment we hear.", "public"),

    ("island-bay", "Community Group", "resident",
     "There's an older gentleman on our street whose ground floor is taking water and he can't manage the step. We've reported it.", "officials"),
    ("island-bay", "Wellington City Council", "official",
     "Received and passed to Free Ambulance for a welfare check. Thank you for flagging it.", "public"),

    ("newtown", "Jo", "resident",
     "Large branch down across Adelaide Road, blocking both lanes.", "public"),
    ("newtown", "Anon", "resident",
     "the council never does anything about this street its a disgrace and the mayor should resign", "public"),
]

BANNER = {
    "level": "warning",
    "text": ("Orange heavy rain warning in force until 8pm. Surface flooding on "
             "Hutt Road at Ngauranga — southbound lane closed. In an emergency call 111."),
}

# The Newtown message above is seeded, then flagged, so the demo can show what
# moderation looks like: it leaves the public feed, a visible marker stays, and
# the flag itself is a signal in the log. Nothing disappears without trace.
FLAG_LAST_NEWTOWN_REASON = "Off-topic for an emergency board — moved out of the public feed, not deleted."
