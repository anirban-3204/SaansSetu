SCHEMA_VERSION = "0.4"   # version of the documents the dashboard reads

# An hourly reading stamped T is the average over the hour that STARTS at T. Checked against OpenAQ's copy of the
# same CPCB stations on 7 Oct 2026 (probe_live.py): three stations out of three match when stamped by hour start.
# So a reading stamped 06:00 covers 06:00 to 07:00 and does not exist until 07:00.
READING_MINUTES = 60
