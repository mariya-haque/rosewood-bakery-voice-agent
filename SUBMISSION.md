# lablab.ai submission kit

Deadline: **September 30, 2026**. Everything below maps to a field on the lablab submission form or to one of the four judging criteria: Application of Technology, Presentation, Business Value, Originality.

---

## Form fields

**Project title**
Rosewood: the AI order line that knows your regulars

**Short description**
A voice agent that answers a small bakery's phone: takes custom cake orders against live stock, greets regulars by name, routes allergy questions to a human, and shows the owner what callers wanted and couldn't buy.

**Long description**

Small food shops lose orders on the phone at exactly the wrong moment. The rush is when nobody can pick up, and studies of small businesses find most calls go unanswered, with most of those callers never trying again.

Rosewood is a phone line that answers every call. Built on the AssemblyAI Voice Agent API, it takes real orders: it checks live stock and prices through HTTP tools before agreeing to anything, offers an alternative when a cake is sold out, finds a pickup slot the kitchen has capacity for, reads the order back, and puts it on the owner's live board with an order number.

It knows the regulars. On a phone call, a pre-connect request looks up the caller's number before the call is answered, so Dana hears "Rosewood Bakery, hi Dana. Is it the butter croissant again?" In the browser it asks for the number first, the way a pizza shop does.

It knows its limits. Allergy and dietary questions, complaints and catering go to the owner as a call-back with a written reason, and a dietary request like "gluten-free cupcakes" is never fuzzy-matched to the ordinary ones.

The part most voice agents skip is what happens after the call. Every sold-out or off-menu request is logged, so the owner sees "gluten-free cake, asked 6 times this week". A signed AssemblyAI webhook fires on every hang-up. The backend pulls the session timeline from the Sessions API and has the LLM Gateway write an outcome, caller sentiment, a one-line summary and a next step. It also records how fast the agent replied, per turn, from AssemblyAI's own timings. When the owner adds a new item to the menu, its name is pushed to the agent's speech recognition as a keyterm, so it's heard correctly on the next call.

AssemblyAI features used: Voice Agent API (browser and Twilio SIP), six HTTP tools with JSON-Schema hints, pre-connect requests, keyterms kept in sync with the menu, a transcription prompt, Voice Focus, webhooks, the Sessions API and the LLM Gateway. A call costs about 7.5 cents a minute; a cake order is worth $40 to $100.

The same design fits any shop that takes orders by phone: a pharmacy, a florist, a tailor.

**Technology tags:** AssemblyAI, Voice Agent API, Universal-3.5 Pro Streaming, LLM Gateway, Python, FastAPI, SQLite, Streamlit, Twilio

**Additional information / notes for judges**

> **How to try it.** Open https://rosewood-bakery.streamlit.app in desktop Chrome or Edge, go to the *Call the bakery* tab, press **Start call** and allow the microphone. Headphones stop the agent hearing itself through your speakers. No sign-up and no API key needed.
>
> **If the app is asleep.** Streamlit's free hosting pauses idle apps. If you see "This app has gone to sleep", click the wake-up button and wait about a minute. The sidebar's *Phone line status* turns green when the agent is connected.
>
> **Things to try:**
> - "Two kilos of red velvet for tomorrow at four, write Happy Birthday Mira." You'll see each tool call in the call window, then the order lands on the *Live board*.
> - "Do you have black forest cake?" It's sold out on purpose: the agent offers an alternative and the request appears under *Missed demand*.
> - "Are your cupcakes gluten free?" The agent won't guess on allergies and puts you on the owner's call-back list instead.
> - Give **415 555 0142** as your phone number to be greeted as Dana, a returning customer, with her usual order.
> - After ordering, ask "is my order ready?" and read out the order number the agent gave you.
>
> **Good to know:**
> - The shop runs on Pakistan time (Asia/Karachi) and is open 9am to 7pm. "Today" and "tomorrow" mean the shop's day, and custom cakes need 90 minutes' notice, so the agent may offer the next open slot instead.
> - The shop data is invented demo data, shared by everyone testing, so you may see other judges' orders on the board.
> - Each call's AI review (outcome, sentiment, summary, next step) appears under *Insights* about one to two minutes after you hang up.
> - Calls are processed by AssemblyAI and kept in the session history for the review. Please don't give real personal details; any name and number will do.
> - The agent pauses for a second or two while it checks stock or pickup slots. That's a live lookup against the shop's database, not a stall.
>
> **Built on.** The project starts from AssemblyAI's open-source voice-agent-starter-python (MIT). The bakery agent, backend, owner dashboard, call reviews and Streamlit demo are ours; the LICENSE lists which files are AssemblyAI's.

**Category tags:** Voice AI, Small Business, Food & Beverage, Customer Service, Analytics

**Public GitHub repository:** https://github.com/mariya-haque/rosewood-bakery-voice-agent. **Demo application platform:** Streamlit. **Application URL:** https://rosewood-bakery.streamlit.app

---

## Demo video (about 3 minutes)

Record the dashboard on the left and the call page on the right, or one screen with the Live tab. Talk over it in short sentences. The **bold** line in each beat is the one to say out loud.

| Time | On screen | Say | Criterion |
| --- | --- | --- | --- |
| 0:00–0:15 | Rosewood dashboard, empty *New* column | **"Most small-business calls go unanswered, and most of those callers never call back. For a bakery, that's a lost cake order. This is Rosewood."** | Business value |
| 0:15–1:10 | Start call. Order 2 kg red velvet for tomorrow at 4, "Happy Birthday Mira". Ask for black forest along the way. | Point at the green tool rows as they appear. **"Every price and slot comes from the shop's system, mid-sentence."** When black forest is sold out: **"…and the owner just learned someone wanted it."** (Signals panel.) The order lands on the board. | Application of technology |
| 1:10–1:35 | New call, give 415 555 0142 | The agent says "welcome back Dana" and offers her usual. **"On the phone this happens before the call is even answered, with AssemblyAI's pre-connect lookup."** Ideally show a real phone call here. | Originality |
| 1:35–1:55 | Ask "are your cupcakes gluten free?" | The agent offers a call-back. **"It never guesses on allergies. The owner gets a call-back with the reason written down."** | Business value, trust |
| 1:55–2:30 | Insights tab | Missed demand ranked, call-backs, and the call reviewed about a minute after hang-up. **"A signed webhook, the Sessions API timeline, and the LLM Gateway turn every call into a summary, an outcome and a next step, with the agent's real reply latency."** | Application of technology |
| 2:30–2:45 | Menu tab: add "Pain au chocolat" | **"New items are pushed into speech recognition as keyterms, so the next caller is understood."** | Application of technology |
| 2:45–3:00 | README "How it uses AssemblyAI" table | **"About 15 cents per order call. Works for any shop that takes orders by phone."** | Business value |

Tips: record the call audio (system audio plus mic). Do a few practice calls first so the webhook reviews are populated. Keep the cursor still while the agent talks.

---

## Slide deck (7 slides)

1. **Rosewood.** The AI order line that knows your regulars. One line, one screenshot.
2. **The problem.** Most small-business calls go unanswered, and most of those callers don't call back.<sup>1</sup> The rush hour is when no one can answer.
3. **What the caller gets.** Real orders against live stock, regulars recognised, allergies routed to a human.
4. **What the owner gets.** Live board, missed-demand ranking, call-backs, AI reviews of every call. The Insights screenshot goes here.
5. **How it's built.** The architecture diagram from the README, plus the AssemblyAI feature table.
6. **Why it's worth it.** About 7.5¢ a minute against a $40–$100 order. The same design fits any shop that takes orders by phone.
7. **Try it.** Phone number, URL, repo.

<sup>1</sup> 411 Locals 2024 study: 37.8% of calls answered by a person. Check the figure against the original before quoting it on a slide.

**Cover image:** the Insights screenshot cropped to the stat tiles and demand bars, with "Rosewood: an AI order line" overlaid. Or a photo of a bakery counter with a phone and the tagline.

---

## Before you submit

- [ ] **Account model access.** Your AssemblyAI account currently gets a 400 from the LLM Gateway for Claude, GPT and Gemini models; only `qwen3.5-4b-32k-fast` works. The agent now uses AssemblyAI's managed model (`"llm": []`) and reviews use Qwen, so both work today. If you enable billing and gain access, set `SUMMARY_MODEL=claude-sonnet-4-6` and restore the `llm` entry in agents/bakery.jsonc.
- [x] Start the backend and a tunnel, set `BACKEND_URL`, then `AGENT=bakery python publish.py` and `python setup_webhook.py`. Done on Sept 28 with a temporary trycloudflare URL; redo it whenever the tunnel restarts.
- [x] Six simulated calls through the real API (scripts/sim_call.py). They found and fixed an invented-regular bug and a duplicate call-back.
- [ ] **Make a few calls yourself with a real voice and a real mic**, including talking over the agent and changing your mind. Synthetic voices are cleaner than people.
- [ ] Deploy `streamlit_app.py` on Streamlit Community Cloud with the secrets from the README, at the subdomain `rosewood-bakery`. Open it once: the sidebar should say the phone line is live. Open it again shortly before judging, since an idle app sleeps.
- [ ] Optional, and the biggest wow factor: attach a Twilio number (`python deployment/telephony/connect.py`) so judges can call it and hear pre-connect recognition.
- [ ] Fill in the placeholders at the top of README.md: URL, phone number, video link.
- [x] docs/insights.png now shows the six test calls.
- [ ] `git init`, check that `.env` and `*.db` are ignored (they are in .gitignore), push to a public GitHub repo.
- [ ] Confirm the LICENSE holder name, and that you're happy submitting code built on AssemblyAI's starter; the LICENSE notes which files are theirs.
- [ ] **Keep costs in check.** Anyone with the URL or the number can start a session billed to your key. The dashboard also has no login: fine for a judged demo, but reset the database afterwards (delete bakery.db, restart).
