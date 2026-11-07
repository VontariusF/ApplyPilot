
# ApplyPilot agent-protocol dogfood transcript

Product head under test: `5958cf06cb64102b99192760f4e7f1e665d0570e` (origin/main — the harness adds scripts only). Branch head: `374b2b22eb702c34497d1e1bc071b673a3c9b8fc`.

Run directory (throwaway): `/home/user/work/dogfood-run2` · fake claude: `/home/user/work/dogfood-run2/bin/claude` · LLM stub: `http://127.0.0.1:35217/v1` · Chromium: `/home/user/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome`



Rig boundaries, disclosed up front (the deterministic stub-provider rig,

the same seams tests/test_agent_journey.py pins):



- Onboarding interview turns ride a scripted `claude -p` fake (print-mode

  contract pinned by tests/test_onboarding_agent.py).

- Scoring, tailoring, judging, and cover letters make real HTTP calls to a

  localhost OpenAI-compatible stub server (`LLM_PROVIDER=custom`).

- Discovery and enrichment are stubbed at their network seams; the stage

  code, DB writes, and scoring run for real.

- PDFs render through Playwright Chromium; the apply run drives the real

  launcher (Chrome worker + engine subprocess) with the fake engine

  emitting stream-json and the pinned RESULT verdicts.

- Every protocol verb below is the real CLI in a fresh invocation surface;


## 1 · Onboarding: intake → resume → kickoff → agent-led turn

```bash
$ applypilot agent start
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": true,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": false,
    "chars": null,
    "pending": false
  }
}
```

- PASS: start → phase intake

- PASS: start → needs.resume

```bash
$ applypilot agent next
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": true,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": false,
    "chars": null,
    "pending": false
  }
}
```

- PASS: next → phase intake, no question

```bash
$ applypilot agent resume --file /home/user/work/dogfood-run2/resume.txt
```

```json
{
  "phase": "intake",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [],
  "last_agent_reply": null,
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: resume → queued (pending)

- PASS: resume → needs.resume off

- PASS: resume block carries no text by default

```bash
$ applypilot agent start
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Hello! Tell me about your background.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: start → kickoff → interview

- PASS: start → mode choices

```bash
$ applypilot agent agent-led on
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": true
  }
}
```

- PASS: agent-led on → mode agent

```bash
$ applypilot agent send --text "Lead with my data engineering background, please."
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 leading with your background.\n\nHere's what I have so far.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 15
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: send → agent turn lands

- PASS: send → partial draft redacted in the payload — Got it — leading with your background.

Here's what I have s

- PASS: redacted payload carries no resume text

```bash
$ applypilot agent next
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 leading with your background.\n\nHere's what I have so far.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 15
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: prefill bridge: chips carry the extracted draft — 4 chips

```bash
$ applypilot agent answer --question does-not-exist --choice x
```

```
(exit 1)
{
  "error": {
    "code": "unknown_question",
    "message": "unknown question",
    "details": null
  }
}
```

- PASS: refusal envelope code == unknown_question — got 'unknown_question'

```bash
$ applypilot agent agent-led off
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 15
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: agent-led off → mode choices


## 2 · Guided agenda: answer xN, payload-driven

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"full_name","prompt":"What's your full name?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":0,"remaining":15},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `full_name` (free-text)

```bash
$ applypilot agent answer --question full_name --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"education","prompt":"What's your highest education?","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":1,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"education","prompt":"What's your highest education?","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":1,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `education` (choice="Bachelor's")

```bash
$ applypilot agent answer --question education --choice Bachelor's
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"experience_years","prompt":"How many years have you worked in your field?","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":2,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"experience_years","prompt":"How many years have you worked in your field?","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":2,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `experience_years` (choice='0–2')

```bash
$ applypilot agent answer --question experience_years --choice 0–2
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":3,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":3,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `country` (choice='United States')

```bash
$ applypilot agent answer --question country --choice "United States"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":4,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":4,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `work_authorized` (choice='yes')

```bash
$ applypilot agent answer --question work_authorized --choice yes
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":5,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"prefilled":null,"allow_free_text":false,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":5,"remaining":14},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `sponsorship` (choice='yes')

```bash
$ applypilot agent answer --question sponsorship --choice yes
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"permit_type","prompt":"Which best describes your right to work?","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":6,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"permit_type","prompt":"Which best describes your right to work?","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":6,"remaining":13},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `permit_type` (choice='Citizen')

```bash
$ applypilot agent answer --question permit_type --choice Citizen
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"currency","prompt":"Which currency should salary conversations use?","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":7,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"currency","prompt":"Which currency should salary conversations use?","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":7,"remaining":12},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `currency` (choice='USD')

```bash
$ applypilot agent answer --question currency --choice USD
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"number"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":8,"remaining":11},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"number"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":8,"remaining":11},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `salary_band` (choice='60000')

```bash
$ applypilot agent answer --question salary_band --choice 60000
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"start_date","prompt":"When could you start?","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"date"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":9,"remaining":10},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"start_date","prompt":"When could you start?","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"prefilled":null,"allow_free_text":true,"free_text_kind":"date"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":9,"remaining":10},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `start_date` (choice='Immediately')

```bash
$ applypilot agent answer --question start_date --choice Immediately
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":10,"remaining":9},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":10,"remaining":9},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `skills_frameworks` (free-text)

```bash
$ applypilot agent answer --question skills_frameworks --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"skills_tools","prompt":"Which tools do you work with?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":11,"remaining":8},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"skills_tools","prompt":"Which tools do you work with?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":"multiline"},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":11,"remaining":8},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `skills_tools` (free-text)

```bash
$ applypilot agent answer --question skills_tools --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"email","prompt":"What email address should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":12,"remaining":7},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"email","prompt":"What email address should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":12,"remaining":7},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `email` (free-text)

```bash
$ applypilot agent answer --question email --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"phone","prompt":"What phone number should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":13,"remaining":6},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"phone","prompt":"What phone number should applications use?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":13,"remaining":6},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `phone` (free-text)

```bash
$ applypilot agent answer --question phone --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"city","prompt":"What city do you live in?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":14,"remaining":5},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"city","prompt":"What city do you live in?","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":14,"remaining":5},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `city` (free-text)

```bash
$ applypilot agent answer --question city --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":15,"remaining":4},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":15,"remaining":4},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `linkedin_url` (free-text)

```bash
$ applypilot agent answer --question linkedin_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"github_url","prompt":"Link your GitHub profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":16,"remaining":3},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"github_url","prompt":"Link your GitHub profile (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":16,"remaining":3},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `github_url` (free-text)

```bash
$ applypilot agent answer --question github_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"website_url","prompt":"Link a personal website (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":17,"remaining":2},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"website_url","prompt":"Link a personal website (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":17,"remaining":2},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `website_url` (free-text)

```bash
$ applypilot agent answer --question website_url --free-text "Something reasonable"
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":18,"remaining":1},"resume":{"provided":true,"chars":133,"pending":false}}
```

```bash
$ applypilot agent next
```

```json
{"phase":"interview","mode":"choices","current_question":{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","choices":[],"prefilled":null,"allow_free_text":true,"free_text_kind":null},"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":false,"provider_setup":false},"progress":{"answered":18,"remaining":1},"resume":{"provided":true,"chars":133,"pending":false}}
```

- answering `portfolio_url` (free-text)

```bash
$ applypilot agent answer --question portfolio_url --free-text "Something reasonable"
```

```json
{"phase":"review","mode":"choices","current_question":null,"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":true,"provider_setup":false},"progress":{"answered":19,"remaining":0},"resume":{"provided":true,"chars":133,"pending":false},"review_fields":[{"id":"full_name","prompt":"What's your full name?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"education","prompt":"What's your highest education?","value":"Bachelor's","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"experience_years","prompt":"How many years have you worked in your field?","value":"0\u20132","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false,"from_resume":true,"needs_answer":false},{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","value":"United States","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"permit_type","prompt":"Which best describes your right to work?","value":"Citizen","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"currency","prompt":"Which currency should salary conversations use?","value":"USD","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","value":"60000","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"start_date","prompt":"When could you start?","value":"Immediately","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_tools","prompt":"Which tools do you work with?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"email","prompt":"What email address should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"phone","prompt":"What phone number should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"city","prompt":"What city do you live in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"github_url","prompt":"Link your GitHub profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"website_url","prompt":"Link a personal website (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false}],"missing_review_fields":[]}
```

```bash
$ applypilot agent next
```

```json
{"phase":"review","mode":"choices","current_question":null,"prefill_chips":[{"question_id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false},{"question_id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true},{"question_id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true},{"question_id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true}],"last_agent_reply":"Noted \u2014 tell me more when you're ready.","needs":{"resume":false,"confirm":true,"provider_setup":false},"progress":{"answered":19,"remaining":0},"resume":{"provided":true,"chars":133,"pending":false},"review_fields":[{"id":"full_name","prompt":"What's your full name?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"education","prompt":"What's your highest education?","value":"Bachelor's","choices":[{"value":"Bachelor's","label":"Bachelor's"},{"value":"Master's","label":"Master's"},{"value":"PhD","label":"PhD"},{"value":"Self-taught","label":"Self-taught"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"experience_years","prompt":"How many years have you worked in your field?","value":"0\u20132","choices":[{"value":"0\u20132","label":"0\u20132"},{"value":"3\u20135","label":"3\u20135"},{"value":"6\u201310","label":"6\u201310"},{"value":"10+","label":"10+"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"work_style","prompt":"How do you want to work?","value":"remote","choices":[{"value":"remote","label":"Remote"},{"value":"hybrid","label":"Hybrid"},{"value":"onsite","label":"On-site"}],"allow_free_text":false,"from_resume":true,"needs_answer":false},{"id":"country","prompt":"Which country will you be working from / applying to jobs in?","value":"United States","choices":[{"value":"United States","label":"United States"},{"value":"United Kingdom","label":"United Kingdom"},{"value":"Canada","label":"Canada"},{"value":"European Union","label":"European Union"},{"value":"India","label":"India"},{"value":"Other","label":"Other"},{"value":"Not sure yet","label":"Not sure yet"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"work_authorized","prompt":"Are you legally authorized to work in the country you're applying in?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"sponsorship","prompt":"Will you now or in the future need visa sponsorship?","value":"yes","choices":[{"value":"yes","label":"Yes"},{"value":"no","label":"No"},{"value":"not_sure","label":"Not sure"}],"allow_free_text":false,"from_resume":false,"needs_answer":false},{"id":"permit_type","prompt":"Which best describes your right to work?","value":"Citizen","choices":[{"value":"Citizen","label":"Citizen"},{"value":"Permanent resident","label":"Permanent resident"},{"value":"Open work permit","label":"Open work permit"},{"value":"H-1B","label":"H-1B"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"currency","prompt":"Which currency should salary conversations use?","value":"USD","choices":[{"value":"USD","label":"USD"},{"value":"EUR","label":"EUR"},{"value":"GBP","label":"GBP"},{"value":"CAD","label":"CAD"},{"value":"INR","label":"INR"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"salary_band","prompt":"What's the minimum annual salary you'd accept?","value":"60000","choices":[{"value":"60000","label":"$60k \u2013 $80k"},{"value":"80000","label":"$80k \u2013 $100k"},{"value":"100000","label":"$100k \u2013 $120k"},{"value":"120000","label":"$120k+"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"start_date","prompt":"When could you start?","value":"Immediately","choices":[{"value":"Immediately","label":"Immediately"},{"value":"In 2 weeks","label":"In 2 weeks"},{"value":"In 1 month","label":"In 1 month"},{"value":"In 3 months","label":"In 3 months"}],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"target_roles","prompt":"Which job titles should we search for? One per line.","value":"Data Engineer","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"location","prompt":"What city are you based in?","value":"Austin, TX","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_languages","prompt":"Which programming languages do you use?","value":"Python, SQL","choices":[],"allow_free_text":true,"from_resume":true,"needs_answer":false},{"id":"skills_frameworks","prompt":"Which frameworks or libraries do you use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"skills_tools","prompt":"Which tools do you work with?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"email","prompt":"What email address should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"phone","prompt":"What phone number should applications use?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"city","prompt":"What city do you live in?","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"linkedin_url","prompt":"Link your LinkedIn profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"github_url","prompt":"Link your GitHub profile (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"website_url","prompt":"Link a personal website (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false},{"id":"portfolio_url","prompt":"Link a portfolio site (optional)","value":"Something reasonable","choices":[],"allow_free_text":true,"from_resume":false,"needs_answer":false}],"missing_review_fields":[]}
```

- PASS: agenda drained to review — 19 answers


### 3 · Review: fields, edits, keep-editing, back to review, confirm

```bash
$ applypilot agent next
```

```json
{
  "phase": "review",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: next → phase review

- PASS: next → needs.confirm

- PASS: review fields ride the payload

```bash
$ applypilot agent next --reveal
```

```json
{
  "phase": "review",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false,
    "text": "Test Person \u2014 data engineer resume.\nSkills: Python, SQL, Airflow.\nExperience: built analytics pipelines at Example Co, 2020-present.\n"
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": [],
  "resume_facts": {
    "preserved_companies": [],
    "preserved_projects": [],
    "preserved_school": "",
    "real_metrics": []
  }
}
```

- PASS: --reveal surfaces resume_facts

```bash
$ applypilot agent review --set phone=555-0199
```

```json
{
  "phase": "review",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "555-0199",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: review --set lands

```bash
$ applypilot agent keep-editing
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Noted \u2014 tell me more when you're ready.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: keep-editing → back to interview

```bash
$ applypilot agent send --text "Lock it in with everything we discussed."
```

```json
{
  "phase": "review",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Perfect \u2014 here is everything we discussed.\n\nHere is your profile.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "555-0199",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: draft hand-off → review

- PASS: send → fenced draft redacted by default — Perfect — here is everything we discussed.

Here is your pro

- PASS: redacted draft carries no raw profile JSON

```bash
$ applypilot agent keep-editing
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Perfect \u2014 here is everything we discussed.\n\nHere is your profile.\n\n[draft extracted \u2014 fenced text redacted; rerun with --reveal]\n",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: second keep-editing → interview

```bash
$ applypilot agent agent-led on
```

```json
{
  "phase": "interview",
  "mode": "agent",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

```bash
$ applypilot agent agent-led off
```

```json
{
  "phase": "review",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": true,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "review_fields": [
    {
      "id": "full_name",
      "prompt": "What's your full name?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "education",
      "prompt": "What's your highest education?",
      "value": "Bachelor's",
      "choices": [
        {
          "value": "Bachelor's",
          "label": "Bachelor's"
        },
        {
          "value": "Master's",
          "label": "Master's"
        },
        {
          "value": "PhD",
          "label": "PhD"
        },
        {
          "value": "Self-taught",
          "label": "Self-taught"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "experience_years",
      "prompt": "How many years have you worked in your field?",
      "value": "0\u20132",
      "choices": [
        {
          "value": "0\u20132",
          "label": "0\u20132"
        },
        {
          "value": "3\u20135",
          "label": "3\u20135"
        },
        {
          "value": "6\u201310",
          "label": "6\u201310"
        },
        {
          "value": "10+",
          "label": "10+"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "country",
      "prompt": "Which country will you be working from / applying to jobs in?",
      "value": "United States",
      "choices": [
        {
          "value": "United States",
          "label": "United States"
        },
        {
          "value": "United Kingdom",
          "label": "United Kingdom"
        },
        {
          "value": "Canada",
          "label": "Canada"
        },
        {
          "value": "European Union",
          "label": "European Union"
        },
        {
          "value": "India",
          "label": "India"
        },
        {
          "value": "Other",
          "label": "Other"
        },
        {
          "value": "Not sure yet",
          "label": "Not sure yet"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "work_authorized",
      "prompt": "Are you legally authorized to work in the country you're applying in?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "sponsorship",
      "prompt": "Will you now or in the future need visa sponsorship?",
      "value": "yes",
      "choices": [
        {
          "value": "yes",
          "label": "Yes"
        },
        {
          "value": "no",
          "label": "No"
        },
        {
          "value": "not_sure",
          "label": "Not sure"
        }
      ],
      "allow_free_text": false,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "permit_type",
      "prompt": "Which best describes your right to work?",
      "value": "Citizen",
      "choices": [
        {
          "value": "Citizen",
          "label": "Citizen"
        },
        {
          "value": "Permanent resident",
          "label": "Permanent resident"
        },
        {
          "value": "Open work permit",
          "label": "Open work permit"
        },
        {
          "value": "H-1B",
          "label": "H-1B"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "currency",
      "prompt": "Which currency should salary conversations use?",
      "value": "USD",
      "choices": [
        {
          "value": "USD",
          "label": "USD"
        },
        {
          "value": "EUR",
          "label": "EUR"
        },
        {
          "value": "GBP",
          "label": "GBP"
        },
        {
          "value": "CAD",
          "label": "CAD"
        },
        {
          "value": "INR",
          "label": "INR"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "salary_band",
      "prompt": "What's the minimum annual salary you'd accept?",
      "value": "60000",
      "choices": [
        {
          "value": "60000",
          "label": "$60k \u2013 $80k"
        },
        {
          "value": "80000",
          "label": "$80k \u2013 $100k"
        },
        {
          "value": "100000",
          "label": "$100k \u2013 $120k"
        },
        {
          "value": "120000",
          "label": "$120k+"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "start_date",
      "prompt": "When could you start?",
      "value": "Immediately",
      "choices": [
        {
          "value": "Immediately",
          "label": "Immediately"
        },
        {
          "value": "In 2 weeks",
          "label": "In 2 weeks"
        },
        {
          "value": "In 1 month",
          "label": "In 1 month"
        },
        {
          "value": "In 3 months",
          "label": "In 3 months"
        }
      ],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true,
      "from_resume": true,
      "needs_answer": false
    },
    {
      "id": "skills_frameworks",
      "prompt": "Which frameworks or libraries do you use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "skills_tools",
      "prompt": "Which tools do you work with?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "email",
      "prompt": "What email address should applications use?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "phone",
      "prompt": "What phone number should applications use?",
      "value": "555-0199",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "city",
      "prompt": "What city do you live in?",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "linkedin_url",
      "prompt": "Link your LinkedIn profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "github_url",
      "prompt": "Link your GitHub profile (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "website_url",
      "prompt": "Link a personal website (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    },
    {
      "id": "portfolio_url",
      "prompt": "Link a portfolio site (optional)",
      "value": "Something reasonable",
      "choices": [],
      "allow_free_text": true,
      "from_resume": false,
      "needs_answer": false
    }
  ],
  "missing_review_fields": []
}
```

- PASS: agent-led round-trip → review

```bash
$ applypilot agent confirm
```

```json
{
  "phase": "done",
  "mode": "choices",
  "current_question": null,
  "prefill_chips": [
    {
      "question_id": "work_style",
      "prompt": "How do you want to work?",
      "value": "remote",
      "choices": [
        {
          "value": "remote",
          "label": "Remote"
        },
        {
          "value": "hybrid",
          "label": "Hybrid"
        },
        {
          "value": "onsite",
          "label": "On-site"
        }
      ],
      "allow_free_text": false
    },
    {
      "question_id": "target_roles",
      "prompt": "Which job titles should we search for? One per line.",
      "value": "Data Engineer, Analytics Engineer",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "location",
      "prompt": "What city are you based in?",
      "value": "Austin, TX",
      "choices": [],
      "allow_free_text": true
    },
    {
      "question_id": "skills_languages",
      "prompt": "Which programming languages do you use?",
      "value": "Python, SQL",
      "choices": [],
      "allow_free_text": true
    }
  ],
  "last_agent_reply": "Got it \u2014 I'll take it from here. What's your full name?",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 19,
    "remaining": 0
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  },
  "written": {
    "profile": "/home/user/work/dogfood-run2/applypilot/profile.json",
    "profile_md": "/home/user/work/dogfood-run2/applypilot/profile.md",
    "searches_yaml": "/home/user/work/dogfood-run2/applypilot/searches.yaml"
  }
}
```

- PASS: confirm → phase done

- PASS: profile.json written

- PASS: searches.yaml written

- PASS: resume.txt on disk


## 4 · Pipeline: run discover enrich score, then run tailor cover pdf

```bash
$ applypilot run discover enrich score --min-score 7
```

```json
╭──────────────────────────────────╮
│ ApplyPilot Pipeline (sequential) │
╰──────────────────────────────────╯
  Min score:  7
  Workers:    1
  Validation: normal
  Stages:     discover -> enrich -> score
  DB:        0 jobs, 0 pending enrichment

======================================================================
  STAGE: discover — Job discovery (JobSpy + Workday + smart extract + Composio)
  Started: 03:35:24
======================================================================
  JobSpy full crawl...
  Workday corporate scraper...
  Smart extract (AI-powered scraping)...
  Composio (web3.career)...

  Stage 'discover' completed in 0.0s — ok

======================================================================
  STAGE: enrich — Detail enrichment (full descriptions + apply URLs)
  Started: 03:35:24
======================================================================

  Stage 'enrich' completed in 0.0s — ok

======================================================================
  STAGE: score — LLM scoring (fit 1-10)
  Started: 03:35:24
======================================================================

  Stage 'score' completed in 0.1s — ok

======================================================================
      Pipeline Summary      
┏━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Stage    ┃ Status ┃ Time ┃
┡━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ discover │ ok     │ 0.0s │
│ enrich   │ ok     │ 0.0s │
│ score    │ ok     │ 0.1s │
│          │        │      │
│ Total    │        │ 0.1s │
└──────────┴────────┴──────┘

  DB Final State:
    Total jobs:     2
    With desc:      2
    Scored:         2
    Tailored:       0
    Cover letters:  0
    Ready to apply: 0
    Applied:        0
======================================================================
```

- PASS: both jobs scored

- PASS: score==9 for https://jobs.example/alpha — got 9

- PASS: score==9 for https://jobs.example/beta — got 9

- PASS: stub provider saw 2 scoring calls — provider calls so far: [{"kind": "score", "marker": "SCORE_PROMPT"}, {"kind": "score", "marker": "SCORE_PROMPT"}]

```bash
$ applypilot run tailor cover pdf --min-score 7
```

```json
╭──────────────────────────────────╮
│ ApplyPilot Pipeline (sequential) │
╰──────────────────────────────────╯
  Min score:  7
  Workers:    1
  Validation: normal
  Stages:     tailor -> cover -> pdf
  DB:        2 jobs, 2 pending enrichment

======================================================================
  STAGE: tailor — Resume tailoring (LLM + validation)
  Started: 03:35:24
======================================================================

  Stage 'tailor' completed in 2.4s — ok

======================================================================
  STAGE: cover — Cover letter generation
  Started: 03:35:26
======================================================================

  Stage 'cover' completed in 2.4s — ok

======================================================================
  STAGE: pdf — PDF conversion (tailored resumes + cover letters)
  Started: 03:35:29
======================================================================

  Stage 'pdf' completed in 0.0s — ok

======================================================================
     Pipeline Summary     
┏━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Stage  ┃ Status ┃ Time ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ tailor │ ok     │ 2.4s │
│ cover  │ ok     │ 2.4s │
│ pdf    │ ok     │ 0.0s │
│        │        │      │
│ Total  │        │ 4.8s │
└────────┴────────┴──────┘

  DB Final State:
    Total jobs:     2
    With desc:      2
    Scored:         2
    Tailored:       2
    Cover letters:  2
    Ready to apply: 2
    Applied:        0
======================================================================
```

- PASS: tailored resumes written — exampleboard_Analytics_Engineer_6f8529f6_v1.pdf, exampleboard_Analytics_Engineer_6f8529f6_v1.txt, exampleboard_Analytics_Engineer_6f8529f6_v1_JOB.txt, exampleboard_Analytics_Engineer_6f8529f6_v1_REPORT.json, exampleboard_Data_Engineer_942471be_v1.pdf, exampleboard_Data_Engineer_942471be_v1.txt, exampleboard_Data_Engineer_942471be_v1_JOB.txt, exampleboard_Data_Engineer_942471be_v1_REPORT.json

- PASS: cover letters written — exampleboard_Analytics_Engineer_6f8529f6_v1_CL.pdf, exampleboard_Analytics_Engineer_6f8529f6_v1_CL.txt, exampleboard_Data_Engineer_942471be_v1_CL.pdf, exampleboard_Data_Engineer_942471be_v1_CL.txt

- PASS: PDFs rendered — exampleboard_Analytics_Engineer_6f8529f6_v1.pdf, exampleboard_Data_Engineer_942471be_v1.pdf, exampleboard_Analytics_Engineer_6f8529f6_v1_CL.pdf, exampleboard_Data_Engineer_942471be_v1_CL.pdf

- PASS: exampleboard_Analytics_Engineer_6f8529f6_v1.pdf is a real PDF — b'%PDF-'

- PASS: exampleboard_Data_Engineer_942471be_v1.pdf is a real PDF — b'%PDF-'

- PASS: exampleboard_Analytics_Engineer_6f8529f6_v1_CL.pdf is a real PDF — b'%PDF-'

- PASS: exampleboard_Data_Engineer_942471be_v1_CL.pdf is a real PDF — b'%PDF-'

- PASS: queue row updated for https://jobs.example/alpha

- PASS: queue row updated for https://jobs.example/beta


## 5 · Apply: plan → confirm → the pinned batch runs (RESULT lines)

```bash
$ applypilot apply --plan --limit 2
```

```json
{
  "plan_hash": "c1cb86bfdbd45d4b4fb8a0fe9cc63b5cdb8191ce746a7e956ead41cb64590937",
  "day": "2026-11-07",
  "commits_nothing": true,
  "batch_size": 2,
  "min_score": 7,
  "cap": 5,
  "cap_source": "default",
  "applied_today": 0,
  "headroom": 5,
  "estimate_basis": "apply_timeout=300s per job (the run's watchdog budget \u2014 an upper bound)",
  "jobs": [
    {
      "url": "https://jobs.example/alpha",
      "application_url": "https://jobs.example/alpha",
      "title": "Data Engineer",
      "site": "exampleboard",
      "fit_score": 9,
      "estimate_seconds": 300
    },
    {
      "url": "https://jobs.example/beta",
      "application_url": "https://jobs.example/beta",
      "title": "Analytics Engineer",
      "site": "exampleboard",
      "fit_score": 9,
      "estimate_seconds": 300
    }
  ],
  "next": "applypilot apply --i-confirm c1cb86bfdbd45d4b4fb8a0fe9cc63b5cdb8191ce746a7e956ead41cb64590937 (same flags as this --plan run, same UTC day)"
}
```

- PASS: plan commits nothing

- PASS: plan batch == 2

- PASS: plan cap == 5/day default

- PASS: plan hash staged

- PASS: plan jobs == the tailored queue

```bash
$ applypilot apply --i-confirm c1cb86bfdbd45d4b4fb8a0fe9cc63b5cdb8191ce746a7e956ead41cb64590937 --headless
```

```json
Launching Auto-Apply
  Limit:    2
  Workers:  1
  Engine:   claude
  Model:    haiku
  Headless: True
  Dry run:  False

Launching apply pipeline (2 jobs, 1 worker, poll every 60s)...
Ctrl+C = skip current job(s) | Ctrl+C x2 = stop
                                         ApplyPilot Dashboard                                       
┏━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━┳━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━┳━━━━┳━━━━━━
┃  ┃ Job                             ┃  Status   ┃ Time ┃ A… ┃ Last Action          ┃ … ┃ F… ┃  Cost
┡━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━╇━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━╇━━━━╇━━━━━━
│  │ Analytics Engineer @ examplebo… │   DONE    │      │    │ finished             │ 1 │  1 │ $0.0…
├──┼─────────────────────────────────┼───────────┼──────┼────┼──────────────────────┼───┼────┼──────
│  │                                 │           │      │    │ TOTAL                │ 1 │  1 │ $0.0…
└──┴─────────────────────────────────┴───────────┴──────┴────┴──────────────────────┴───┴────┴──────
╭───────────────────────────────────────── Recent Events ──────────────────────────────────────────╮
│ 03:35:29 [W0] Launching Chrome...                                                                │
│ 03:35:32 [W0] Starting: Data Engineer @ exampleboard                                             │
│ 03:35:32 [W0] APPLIED (0s): Data Engineer                                                        │
│ 03:35:32 [W0] Launching Chrome...                                                                │
│ 03:35:35 [W0] Starting: Analytics Engineer @ exampleboard                                        │
│ 03:35:35 [W0] CAPTCHA (0s): Analytics Engineer                                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
Done: 1 applied, 1 failed ($0.002)
Logs: /home/user/work/dogfood-run2/applypilot/logs
```

- PASS: run events narrate APPLIED

- PASS: run events narrate CAPTCHA

- PASS: staged plan consumed

- engine run logs (2): claude_20261107_033532_w0_exampleboard.txt, claude_20261107_033535_w0_exampleboard.txt

```
Opened the posting, completed the application form, and submitted. RESULT:APPLIED
Opened the posting, completed the application form, and submitted. RESULT:APPLIED
Opened the posting, but a captcha blocked submission. RESULT:CAPTCHA — retry later.
Opened the posting, but a captcha blocked submission. RESULT:CAPTCHA — retry later.
```

- PASS: engine narrates RESULT:APPLIED

- PASS: engine narrates RESULT:CAPTCHA

- PASS: alpha recorded applied — {"url": "https://jobs.example/alpha", "apply_status": "applied", "apply_error": null, "apply_attempts": 0, "applied_at": "2026-11-07T03:35:32.544704+00:00"}

- PASS: beta terminal, not applied (CAPTCHA) — {"url": "https://jobs.example/beta", "apply_status": "captcha", "apply_error": "captcha", "apply_attempts": 99, "applied_at": null}

- PASS: beta persists distinct captcha status, attempts pinned — {"url": "https://jobs.example/beta", "apply_status": "captcha", "apply_error": "captcha", "apply_attempts": 99, "applied_at": null}

```bash
$ applypilot status --json
```

```json
{
  "total": 2,
  "by_site": [
    [
      "exampleboard",
      2
    ]
  ],
  "pending_detail": 2,
  "with_description": 2,
  "detail_errors": 0,
  "scored": 2,
  "unscored": 0,
  "score_distribution": [
    [
      9,
      2
    ]
  ],
  "tailored": 2,
  "untailored_eligible": 0,
  "tailor_exhausted": 0,
  "with_cover_letter": 2,
  "cover_exhausted": 0,
  "applied": 1,
  "captcha": 1,
  "apply_errors": 0,
  "ready_to_apply": 1
}
```

- PASS: status --json counts 1 applied — applied=1, total=2

- PASS: status --json counts captcha separately from failures — captcha=1, apply_errors=0

```bash
$ applypilot apply --i-confirm c1cb86bfdbd45d4b4fb8a0fe9cc63b5cdb8191ce746a7e956ead41cb64590937
```

```
(exit 1)
{
  "error": {
    "code": "plan_required",
    "message": "no plan is staged \u2014 run 'applypilot apply --plan' first, then confirm with the plan hash it printed."
  }
}
```

- PASS: refusal envelope code == plan_required — got 'plan_required'


## 6 · Closing probes: redo wipes the interview, profile survives

```bash
$ applypilot agent redo
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Hello! Tell me about your background.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: redo → fresh interview (observed semantics) — phase=interview, answered=0

- PASS: redo resets the agenda

- PASS: profile.json survived redo

```bash
$ applypilot agent next
```

```json
{
  "phase": "interview",
  "mode": "choices",
  "current_question": {
    "id": "full_name",
    "prompt": "What's your full name?",
    "choices": [],
    "prefilled": null,
    "allow_free_text": true,
    "free_text_kind": null
  },
  "prefill_chips": [],
  "last_agent_reply": "Hello! Tell me about your background.",
  "needs": {
    "resume": false,
    "confirm": false,
    "provider_setup": false
  },
  "progress": {
    "answered": 0,
    "remaining": 18
  },
  "resume": {
    "provided": true,
    "chars": 133,
    "pending": false
  }
}
```

- PASS: next after redo → fresh guided interview


## 7 · Provider call ledger

- provider calls by kind: `{"score": 2, "tailor": 2, "judge": 2, "cover_letter": 2}`

- fake-claude turns logged: `/home/user/work/dogfood-run2/claude-prompts.log`


## Verdict

All checks passed — every protocol verb ran end-to-end; every error

in this transcript is an intentional, labeled probe of the error

envelope (unknown-question refusal, burned-token replay refusal).
