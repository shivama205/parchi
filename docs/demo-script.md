# Demo video — shot list

**Hard requirements from the rules.** Maximum 4 minutes. Must show *unedited, live
execution* of the agent doing its task. Must show *visible proof the backend runs
on Google Cloud* — console, dashboard or logs on screen. English, or English
subtitles. No third-party trademarks beyond what the constructed documents carry.

**Total budget: 240 seconds.** Timings below are generous by ~15s so an overrun
in one beat does not force a re-record.

---

## Before you press record

```bash
cd ~/Projects/Hobby/parchi
./.venv/bin/python -m pytest -q            # confirm 355 green
./.venv/bin/python make_documents.py       # regenerate the 13 printed documents
```

Reset the demo patient so the timeline assembles on camera rather than
appearing pre-populated:

```bash
curl -s -X DELETE https://parchi-638690742795.asia-south1.run.app/api/patient/demo | head -c 200
```

Open these five tabs in this order, so you never hunt for one on camera:

1. `https://parchi-638690742795.asia-south1.run.app` — the caregiver chat
2. `https://parchi-638690742795.asia-south1.run.app/d/p-fixture-1` — the prescriber view
3. Cloud Run → service `parchi` → **Revisions** tab
4. Firestore → **Data** → `patients/p-fixture-1`
5. Cloud Scheduler → `parchi-sweep` → with the **Logs** pane open

Have a Finder window with `fixtures/printed/` ready to drag from, and a terminal
sized large enough to read at 1080p.

**One warning.** The duplicate-molecule finding does not survive every cold
upload — a single low-agreement reading pushes Dr Rao's June script below the
0.6 rewrite threshold, at which point the system correctly declines to claim a
stop. Shoot the *upload* on the `demo` patient and the *findings* on the seeded
`p-fixture-1`. Say on camera which is which; it is a stronger moment than
pretending, and the reason is a good one.

---

## 0:00 — 0:25 · The problem

**On screen:** the Finder window of thirteen documents, scrolling.

> "My father sees four doctors who have never spoken to each other. Each writes
> brand names. Nobody — including him — knows what he is actually taking. This is
> the folder."

Do not explain the architecture yet. Establish the person.

---

## 0:25 — 1:05 · J1 · The folder dump *(live)*

**On screen:** tab 1. Drag all thirteen files in at once, deliberately unsorted.

> "Thirteen documents, no order, no labels, mixed prescriptions and lab reports."

Let the response land visibly — the upload returns in under a second, then
statuses move `queued → reading → ready` while you talk.

> "The upload came back immediately; reading happens behind it. Each document
> goes on the timeline by the date printed *on the page* — not when I uploaded
> it. Anything with no legible date is listed separately rather than quietly
> filed under today."

**Beat to land:** the timeline in date order, 2025 through 2026, with
prescriptions and lab reports interleaved. That is **AC-1**.

---

## 1:05 — 1:45 · The duplicate nobody saw *(seeded patient)*

**On screen:** switch to `p-fixture-1` in tab 1. Point at the questions list.

> "Here is the one that matters. The cardiologist prescribes Ecosprin AV. The
> physician, five weeks later, prescribes Storvas. Different brands, different
> doctors — and both contain atorvastatin."

Read the finding aloud, exactly as it appears:

> *"Is atorvastatin intended twice over, through both Ecosprin AV 75 and
> Storvas 10?"*

> "Note what it does not say. It does not say this is dangerous, or that he
> should stop. It states what is on the paper and asks the question. Every line
> names the document it came from."

**Beat to land:** a finding that is an observation plus a question. That is
**AC-3**, and it is the product's whole argument.

---

## 1:45 — 2:15 · The handwriting it refuses to guess at *(live)*

**On screen:** terminal.

```bash
GOOGLE_CLOUD_PROJECT=ascend-473804 ./.venv/bin/python -m parchi.extract \
  fixtures/external/mirage-100/images/rx-004.jpg
```

While it runs (about six seconds):

> "This is a real handwritten Indian prescription from a public research corpus.
> It reads it three times with three different framings, and the confidence you
> see is not the model's opinion of itself — it is whether the three reads agree.
> I measured that: asked directly, the model said HIGH on three hundred and
> forty-two lines out of three hundred and eighty-two, and LOW eleven times.
> None of those eleven were right."

> "Where the reads disagree, the reading is marked low and **kept off the
> medication list entirely** until a human confirms it."

**Beat to land:** five drugs read off genuinely hard handwriting, with agreement
counts visible. That is **AC-6**.

---

## 2:15 — 2:50 · The brief nobody asked for *(live)*

**On screen:** terminal.

```bash
./.venv/bin/python -m parchi.brief
```

> "No arguments. That is the point."

> "In June, Dr Rao wrote 'review after 68 days' on a prescription. Nobody typed
> that anywhere. The sweep found the date, months later, and built this."

Scroll the six sections. Land on section 1 and section 6.

> "What changed since your last visit. And what has already been tested — so
> nobody re-orders a creatinine that was run eight weeks ago."

**Beat to land:** an appointment brief produced with zero user action. That is
**AC-9**.

---

## 2:50 — 3:20 · The doctor's seven minutes

**On screen:** tab 2, on a phone-width window if you can.

> "Same data, different reader. A prescriber has seven minutes. This opens on six
> numbers and two sections — what changed, what the family is asking — with
> everything else one tap away."

Expand **Lab trends** on camera.

> "HbA1c across four measurements at three laboratories. One of them reports in
> IFCC millimoles per mole, the others in NGSP percent. They are on one axis, and
> every point keeps the reference range printed on *its own* report — because one
> lab's range does not apply to another lab's value."

**Beat to land:** the doctor as a second audience, and **AC-8**.

---

## 3:20 — 3:50 · It really is on Google Cloud *(required)*

Move through tabs 3, 4, 5 without lingering.

**Cloud Run (tab 3):** point at the region and the serving revision.

> "Cloud Run, asia-south1. Mumbai. Scales to zero."

**Firestore (tab 4):** expand `patients/p-fixture-1` → `mentions`.

> "Firestore, same region. It stores documents, mentions and lab results — the
> evidence. It never stores the medication list or the findings. Those are
> recomputed on every request, so they cannot drift from the paper."

**Cloud Scheduler (tab 5):** show the log line.

> "And the sweep that built that brief — 200, user agent Google-Cloud-Scheduler.
> There is no model call behind it. Finding a date in a window is arithmetic."

**This beat is mandatory.** Do not cut it for time; cut from 0:00–0:25 instead.

---

## 3:50 — 4:00 · Close

**On screen:** terminal, one command.

```bash
./.venv/bin/python -m pytest -q -k clinical_claim
```

> "Three hundred and fifty-five tests. This one fails if any output ever tells a
> family what to do. It has never passed by accident — a `Finding` carrying that
> language cannot be constructed at all."

> "Parchi does not diagnose, advise, or claim two drugs interact. It reads the
> paper, and it asks better questions."

---

## If you overrun

Cut in this order:

1. **0:00–0:25**, the problem framing — trim to one sentence over the folder.
2. **2:50–3:20**, the prescriber view — it is the newest work and the least
   load-bearing for the acceptance criteria.
3. **1:45–2:15**, the handwriting — but only if you have already shown a
   low-confidence reading somewhere else on screen.

Never cut the Google Cloud beat, and never cut the duplicate-molecule finding.
One is a stated requirement; the other is the reason the project exists.

---

## Say these words somewhere

The rules score honesty about data as much as capability, and two sentences
cover it:

> "Every document you have seen is constructed — invented patient, invented
> doctors, invented clinics. The handwriting comes from a public research corpus
> under a no-derivatives licence, which is why you see a highlight box over the
> full page rather than a crop."

> "The brand table is demo-grade: seventy-three brands, resolving about a fifth
> of the drug names in that corpus. Everything it cannot resolve becomes a
> question rather than a guess."
