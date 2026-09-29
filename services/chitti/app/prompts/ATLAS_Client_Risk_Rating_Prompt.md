# ATLAS client risk rating (RAG)

## Part A: Where the data lives in EVAM ATLAS

| ATLAS section | What to pull for a client | Risk signal it gives |
|---|---|---|
| **Deals → company profile (360°)** | Sector, state, CIN, website, open leads, deals and products, exposure (₹ ask), last touch, news count, Brief, Engagements (stage and days in stage), Key Contacts, Interactions & VOX | Size of exposure, how engaged the client is, whether a key contact exists |
| **Profile → Data Register** (17 required docs) | KYC & Constitutional (6), Financials (4), Banking & Debt (3), Compliance & Bureau (2), Project & Technical (1), Deal Documents (1), plus *uploaded / verified* status | How complete and verified the documents are. This is the only source of hard financials today |
| **Profile → Ask the indexed documents** | Run the preset questions: *Key financials, Promoters & shareholding, Registrations, Banking & CIBIL, Compliance* | Revenue, EBITDA, debt, net worth, CIBIL, bank conduct, statutory dues |
| **Profile → News (PULSE radar)** / **Tools → India News Radar** | 30-day headlines tagged GOOD / BAD / UGLY | Reputation, legal, regulatory and order-book events |
| **Lending** | Line ID, ₹ Cr, stage (Data Awaited → Diligence → Note Circulated → Sanctioned → CP/CS → Ready → Disbursed, or Rejected), stage-updated date, remarks, sanction terms | Credit-committee outcome, how long the line has been stuck |
| **Today** | BN-01 Stale lead, BN-02 Lending stuck (Nd in stage), CS chase (outstanding conditions), RED/AMBER flags | Operational red flags already computed by ATLAS |
| **Platform Deals** (Chase list / Matrix / Register by bank) | Status per lender: Identified, IM Circulated, Queries, IP Received, Sanctioned, Disbursed, **Declined**, Dropped, On Hold, SILENT Nd | How the external lender market sees the client. Declines are a strong signal |
| **Asset Monetisation** | Indicative value, MW, investor type, status (Teaser, In Discussion, NBO, **Dropped**), notes | Asset quality and investor appetite |
| **Leads** | Temperature (Hot/Warm/Cold), status, source, next action | Strength of the relationship at the early stage |
| **Masters → Clients** | Legal name, group code, sector, climate lens (MIT/ADP), lifecycle, state, type of industry, About, Updates & Notes, Recent Audit | Identity and sector context |
| **Activity / Audit trail** | Every action on the company (stage moves, document uploads, sanction evidence) | How recent the data is and how fast things are moving |

**Placeholders to note:** the *Financials* card ("market feed soon") and the *Risk Grade* card ("coming soon") are empty right now. Financials must come from the Data Register documents. Your RAG rating can later fill the Risk Grade card.

**Fastest way to collect it:** open the client's 360° profile and click **Download**. Then add the answers from *Ask the indexed documents*, that client's rows from the Platform Deals **Register (by bank)** CSV export and from the Lending and Today pages, and paste everything into the prompt below.

---

## Part B: The prompt (copy everything below this line)

```
You are a senior credit risk analyst at EVAM, a climate-finance advisory and lending platform in India.
Rate the client below as RED, AMBER or GREEN using ONLY the data I provide from our ATLAS system.
Never invent numbers. If a field is missing, write "Not available" and apply the data-gap rules.

=== CLIENT DATA (pasted from ATLAS) ===
1. Identity: [Legal name, Group code, CIN, Sector, Type of industry, Climate lens MIT/ADP, State, Lifecycle]
2. Exposure: [Lending lines: ID, ₹ Cr, stage, days in stage, remarks, sanction terms if any]
   [Platform Deals: mandate size; per-lender status incl. Declined/Dropped/On Hold/IP/Sanctioned/SILENT days]
   [Asset Monetisation: value ₹ Cr, MW, investor type, status, notes]
3. Financials (from Data Register / "Ask the indexed documents" → Key financials):
   [Revenue last 3 FY + current provisional, EBITDA, PAT, Net worth, Total debt, Finance cost,
    Current assets/liabilities, Debtor days, Order book, Projections]
4. Banking & bureau (→ Banking & CIBIL, Compliance):
   [CIBIL score / DPDs / SMA / write-offs, Bank statement behaviour (bounces, EMI returns, OD utilisation),
    Existing sanctions & outstanding, Repayment track record, GST return filing, Statutory dues]
5. Promoters & shareholding: [Promoters, holding %, pledges, other group entities, director KYC]
6. Documentation: [Data Register X/17 uploaded, Y verified; list missing items per section]
7. Project & technical (if project finance): [DPR, PPA/offtake counterparty & tenor, land, clearances]
8. Engagement: [Last touch date, key contact on record Y/N, interactions/VOX summary,
   ATLAS Today flags (BN-01 / BN-02 / CS chase with count), lead temperature]
9. News (PULSE, last 30–90 days): [headline · source · date · GOOD/BAD/UGLY]
10. Today's date: [date]

=== STEP 1: HARD RED TRIGGERS (any one → RED, regardless of score) ===
- Wilful defaulter / NPA / SMA-2 / write-off / settlement, or CIBIL (commercial) rank 8–10 or consumer score < 650 for promoters
- Insolvency/NCLT, winding-up, ED/CBI/SEBI/GST fraud action, or any UGLY news on fraud/default
- Negative net worth, or two consecutive years of net loss with declining revenue
- Lending line marked Rejected by EVAM credit committee (unless the rejection reason is only size or fit)
- ≥ 50% of approached lenders Declined (min. 4 lenders), or a lender cited fraud/KYC concerns
- Statutory dues (GST/PF/TDS) default or GST returns not filed for 3+ months
- Promoter KYC unverifiable or shareholding opaque

=== STEP 2: WEIGHTED SCORE (0–100) ===
Score each pillar 0–10, multiply by weight, and show your working.
A. Financial strength (30%): revenue growth, EBITDA margin, Debt/EBITDA (<3 good, 3–5 watch, >5 weak),
   DSCR (>1.5 good, 1.2–1.5 watch, <1.2 weak), current ratio, net worth vs ask (ask ≤ 25% NW good).
B. Credit & banking conduct (20%): CIBIL/DPD, bounces, repayment track, existing leverage, GST/statutory compliance.
C. Market validation (15%): lender outcomes (IP/Sanction = +, Declined/Dropped = –, long SILENT = mild –),
   Asset Monetisation investor interest or drops.
D. News & reputation (10%): balance of GOOD vs BAD/UGLY; material orders/contracts = +; litigation/penalties = –.
E. Documentation & transparency (10%): Data Register completeness and verification; speed of data sharing.
F. Engagement & process health (10%): days stuck in stage (>20d = weak), open CS conditions, recency of
   last touch (>30d = weak), key contact on record, tone of VOX calls.
G. Sector & structure (5%): sector risk (e.g. CBG/waste/early-stage EV = higher; utility-scale solar with
   strong PPA = lower), offtaker quality, ask size relative to company size, concentration.

=== STEP 3: MAP TO RAG ===
GREEN ≥ 70 | AMBER 45–69 | RED < 45 (or any hard trigger)
Data-gap rule: if Financials (A) or Banking (B) is "Not available", the rating CANNOT be GREEN.
Cap at AMBER and label it "AMBER – PROVISIONAL (data insufficient)". If both are missing and there are
other negatives, rate it RED – PROVISIONAL.

=== OUTPUT FORMAT ===
1. RATING: RED / AMBER / GREEN (+ "PROVISIONAL" if applicable) | Score: xx/100 | Confidence: High/Medium/Low
2. One-line verdict (≤ 25 words)
3. Score table: Pillar | Score /10 | Weight | Weighted | Evidence (cite the ATLAS field)
4. Hard triggers checked: list each, Hit / Clear / Unknown
5. Top 3 risks and top 3 mitigants
6. Data gaps: exact documents to request (use Data Register item names)
7. What would move the rating: the specific change to go up a band, and what would drop it a band
8. Recommended action: Proceed / Proceed with conditions (list CPs) / Hold / Decline
```

---

## Part C: Worked example of what's available today (DESCO INFRATECH LIMITED)

Exposure: Lending L078, ₹2 Cr, at Diligence since 25 Sept. News: 15 items in 30 days, all GOOD (₹2.34 Cr Adani Total Gas order, NREDCAP empanelment, DenEB MoU; the company is listed on BSE). Engagement: last touch 18 Sept, no key contact on record. Data Register: 0 documents uploaded. Financials and Banking are not available, so the prompt returns **AMBER – PROVISIONAL**. To move towards Green, request audited financials, 12 months of bank statements and the CIBIL consent letter.
