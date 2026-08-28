# The model said HIGH on 342 lines out of 382

*Four things I measured while building a health-record agent, each of which contradicted the spec I had written a week earlier.*

---

I spent five days building [Parchi](https://github.com/shivama205/parchi), an agent that reads a family's medical paperwork and produces two things nobody currently maintains: an accurate account of what an ageing parent is actually taking, and a list of questions worth asking at the next appointment.

I wrote a product spec before I wrote any code. Every one of the four findings below contradicted it. That is not a complaint about the spec — it is the argument for measuring early, because each of these was cheap to discover on day one and would have been expensive to discover on day five.

## 1. Asking a model how confident it is does not work

The whole product rests on one distinction. Indian prescriptions are handwritten, and a wrong drug on a medication list is the worst thing this software can produce. So the design was: read the document, and where the reading is uncertain, show it to the caregiver for confirmation rather than putting it on the list.

My spec said to ask the model for a per-field confidence and gate on it. That is the obvious design. It is also useless.

I ran 85 handwritten Indian prescriptions from a public research corpus through Gemini 3.5 Flash, asking for a confidence on every medication line. The result:

- **HIGH on 342 of 382 lines** — including 88 that matched no drug in the ground-truth annotation at all
- **LOW eleven times** — and not one of those eleven was a correct reading

It essentially never expresses doubt, and when it does, it is wrong about what to doubt. A gate built on that signal would have gated nothing while appearing to work.

What replaced it: **confidence derived from agreement between independently framed reads.** The document is read three times with materially different prompts — one asks for a list, one walks the page line by line, one asks the model to act as a pharmacist dispensing from the script. Unanimous is high. A majority is medium. A lone dissenting read is low, and low never reaches the medication list.

The subtlety that matters: **independence has to come from the prompt, not the temperature.** Three reads at temperature zero sharing one prompt are one read repeated, and their agreement means nothing at all.

## 2. Thinking cost 22× for a gain that landed once in ten

The obvious next question was whether to let the model think. I measured it on the same ten images.

| Configuration | Recall | Cost/image | Wall clock, 10 images |
|---|---|---|---|
| One cheap read | 74.3% | $0.0033 | — |
| Two reads + thinking escalation | 80.0% | $0.0680 | 219s |
| Always thinking | 82.9% | $0.0729 | 284s |
| **Three cheap reads** | **82.9%** | **$0.0162** | **20s** |

Thinking bought 8.6 points of recall. But the entire gain came from **one image in ten** — on the other nine the output was identical. It is not a quality dial you turn up. It is occasionally decisive and usually waste.

Two operational details that cost me real money to learn:

**Thinking spend is bimodal, not uniform.** Median 1,840 tokens; maximum 62,910. My first measurement happened to land on a 62,911-token outlier, which made thinking look 150× more expensive than it actually averages. I reported that number before I had a distribution, and had to correct it. Measure across many inputs or you will be wrong by an order of magnitude in either direction.

**A nonzero `thinkingBudget` is silently ignored.** I set 256 and then 1024 on the same image. Both produced ~62,911 thinking tokens. Only exactly `0` disables it — the cap is not a cap, and the field behaves as a boolean. Which means when thinking runs, you cannot bound what it costs.

That was decisive. One escalation in ten images cost **$0.58 on its own** — the entire monthly budget for one patient. A third cheap prompt matched always-thinking's recall at a quarter of the cost and a fourteenth of the wall time, and calibrates better besides: it flags 15 lines in 42 as uncertain, where the escalation design flagged one in 35.

Also worth saying plainly: **thinking tokens bill as output.** They arrive in a separate field (`thoughtsTokenCount`), so a cost model built on `candidatesTokenCount` understates the bill by orders of magnitude and looks fine while doing it.

## 3. One real document found bugs a hundred synthetic ones could not

I built a careful test corpus by hand: one patient, three prescribers, fourteen months, containing every case the reconciliation logic needed to handle. 291 tests passing.

Then I downloaded one real prescription image and looked at it. Before writing any extraction code, that single document surfaced three parser bugs:

| Written as | Parsed as | Consequence |
|---|---|---|
| `DOLO TAB 650MG` | unresolvable | Form word mid-string. **0 of 344** drug names resolved |
| `Volix (0.3mg)` | `0` and `3` | Decimal point stripped as punctuation — a tenfold strength error |
| `Telma 40 1 OD` | `40mg` and `1mg` | The quantity counted as a strength, discarding the real 40 mg |

Two more came from the first live runs. The instructive one is `T Allegra 120mg` — `T` is the standard Indian abbreviation for tablet, I only handled `Tab`, and every such line silently failed to resolve.

That bug **never appeared in any accuracy metric**, because the ground-truth annotations write `ALLEGRA TAB 120MG`. The defect lived only in what the model *emits*, not in what the labels say. Validating against labels alone would have shipped it.

The lesson is narrower and more useful than "test with real data": **validate against real model output, not only against ground truth.** They differ, and the gap is where bugs hide.

## 4. A claim is not a name

The product has one absolute rule: it never says anything clinical. It states what is on the paper and pairs it with a question for the doctor. To make that structural rather than aspirational, a `Finding` object cannot be constructed at all if its text contains clinical-claim vocabulary — a list including "you should", "dangerous", "stop taking", and the stem "diagnos" so that "diagnosis" and "diagnostic" cannot slip through.

Then the first real laboratory name reached a finding, and reconciliation crashed.

A major Indian diagnostic chain is called **SRL Diagnostics**. "Diagnostics" contains "diagnos". Most Indian labs are named exactly that way. Any patient using one would have brought the service down.

The fix is the interesting part, because the wrong fix is easy: mangle the name, or weaken the invariant. Neither is right. The invariant exists to stop the software *asserting* something clinical, not to stop it saying where a test was run. So quoted text is now handled in two kinds:

- **A proper noun we matched** — a laboratory, a prescriber, a brand in our table — is declared, masked before the scan, and shown intact.
- **A reading that resolved to nothing** is arbitrary model output about an unreadable scrawl and could say anything, so forbidden vocabulary in it is redacted.

I briefly collapsed those two into one and made the redaction unconditional. A test caught it. That change would have printed advice-shaped words to a worried caregiver on the strength of an OCR guess — and quotation marks are not much defence when someone is skimming a phone at a bus stop.

## The thing I did not build

The most requested feature I turned down was adherence tracking: has the patient been skipping doses?

We only ever see paper. A prescription records what was *prescribed*, never what was *taken*. A gap between scripts could be a switched pharmacy, a sample pack from the clinic, a hospital admission, or a strip bought over the counter. Inferring skipped doses from documents would be precisely the confidently-wrong claim the rest of the design exists to prevent.

What exists instead is the honest version: *"metformin was last written 200 days ago with no end date — is it still being taken?"* An observation about paper, paired with a question.

## What generalises

The judgement layer of this product contains no model call. Deciding whether a drug was stopped, whether two products overlap, whether a lab trend exists — that is deterministic Python with 355 tests. The model reads pixels into text; everything downstream is arithmetic.

That was not asceticism. It is the only way the safety properties hold: an invariant you can only check *after* generation is not an invariant. And it paid off somewhere I did not design for — because the reconciliation function is pure and takes the current date as an argument, "what changed since your last visit" needed no new code at all. It is the same function run over the documents that existed then.

If there is one line to take from five days of this, it is the one that keeps recurring in different costumes: **the model's report about itself is not evidence.** Not its confidence, not its token accounting, not its claim to have read the whole page. Agreement between independent attempts is evidence. Ground truth is evidence. A test that fails is evidence.

And one limit I could not engineer away, which belongs in any honest write-up: agreement detects disagreement, not shared omission. A drug that all three reads miss produces no line at all, so it cannot be flagged. At 82.9% recall that is roughly one drug in six. A caregiver must never be told the list is complete.

---

*I built Parchi and wrote this post for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/). The code is at [github.com/shivama205/parchi](https://github.com/shivama205/parchi) and the running service is [here](https://parchi-638690742795.asia-south1.run.app). Every patient, prescriber, clinic and laboratory in the project is invented; handwriting samples come from a publicly released subset of the MIRAGE corpus (arXiv:2410.09729) under CC BY-ND 4.0. No real patient data appears anywhere in it.*
