# Follow-up questions evaluation — baseline

Run 2026-11-18 10:18 against http://127.0.0.1:8011, 7.1 minutes. 30 questions asked; every suggestion clicked with the conversation as history, and any that did not get a real answer clicked a second time.

## In plain language

- **Suggestions offered:** 21/24 (88%) of normal answers came with suggestions; 1 were dropped for arriving too late.
- **Catalog counts:** 1/3 came with 'List them / Break them down' suggestions.
- **Controls** (a greeting, an out-of-scope question): 0 came with suggestions (should be 0).
- **Speed:** suggestions appeared a median 0.74 s after the answer (90% within 2.7 s).
- **Clicked suggestions that got a real answer:** 59/63 (94%); another 0 answered only on the second try.
- **Half-answered** (related material, but not what was asked): 4.
- **Backfired** (refused, asked to clarify, or missed the question twice): 0/63 (0%).
- **Already answered by the answer above them:** 14/63 (22%).
- **How specific:** broad 28, focused 26, narrow 9.

Catalog suggestions clicked: 'List them' → partly; 'Break them down by theme' → answered

## Suggestions that did not cleanly work

### PARTLY: How does TERI turn research into practical solutions?
- Under: *What research does TERI do?*
- First click: refusal, judged 'no' — The clicked reply provides no substantive explanation and only says the information is unavailable, while the original answer already described how TERI develops, tests, and implements research-based solutions.
- Second click: answered, judged 'partly' — The reply describes TERI’s research-to-implementation work and gives examples, but says it does not explain the specific steps; the original answer already covered this by describing technology development, testing, field implementation, and practical interventions.
- Reply: I don't have information on that in the available sources.

### PARTLY: What renewable-energy potential did TERI assess in West Bengal?
- Under: *What is clean energy?*
- First click: answered, judged 'partly' — The reply gives an eastern-region estimate and says West Bengal’s own potential is not stated, so it does not provide the specific figure asked for.
- Second click: answered, judged 'partly' — The reply gives a regional clean-energy estimate and explains that it lacks a separate West Bengal figure, so it does not provide the specific state-level potential asked for.
- Reply: TERI’s source gives a figure for the eastern region, not specifically for West Bengal: the region’s assessed clean-energy generation potential was 88 GW, with solar accounting for almost two-thirds [5]. It does not state West Bengal’s own renewable-energy potential. In February 2025, TERI and West Bengal’s renewable-energy department identified floating solar, agri-PV and building-integrated photovoltaics among areas for collaboration [6].

### PARTLY: What is Dr Vibha Dhawan’s academic background and current teaching role?
- Under: *Tell about the director general of TERI*
- First click: answered, judged 'partly' — The reply identifies her current adjunct teaching role but says her academic background is unavailable, and the original answer already listed the same adjunct professorship and her research and publication credentials.
- Second click: answered, judged 'partly' — The reply identifies her current adjunct professorship but says her academic qualifications are not available, leaving part of the question unanswered.
- Reply: Dr Vibha Dhawan’s academic background is not specified in the available sources. Her current teaching role is Adjunct Professor at Consul General South Asia Partnership, Michigan State University [1]. The sources also note her deep academic and research interests, six books and more than 50 publications, but do not give details of her degrees or formal education [1].

### PARTLY: What environmental assessment was conducted for the Rewa solar project?
- Under: *What are the environmental impacts of solar energy?*
- First click: answered, judged 'partly' — The reply explains that each Rewa sub-project required an ESIA and an environmental management plan, but it does not describe the assessment’s specific findings or mitigation measures.
- Second click: answered, judged 'partly' — The reply explains that Rewa subprojects required an ESIA and project-specific management plans, but does not provide the assessment’s methods or findings.
- Reply: The Rewa Solar Park’s environmental and social impact assessment (ESIA) was required for each sub-project, with a project-specific environmental management plan integrated into the contract with Rewa Ultra Mega Solar Limited (RUMSL) [4]. The assessment formed part of the project’s environmental and social management framework [4]. The assessment requirement was applied through RUMSL’s agreements with developers, who were also required to set aside one percent of profit after tax for socio-econom…

### PARTLY: List them
- Under: *How many events did TERI hold in 2025?*
- First click: answered, judged 'partly' — The reply lists only the 10 most recent of 116 events, rather than listing them all.
- Second click: answered, judged 'partly' — The reply lists only the 10 most recent of 116 events, so it does not provide the full list implied by “List them.”
- Reply: Here are the 10 most recent of **116 events** in 2025: - [Future Fuel Pathways under a Delayed Net Zero Framework](https://teriin.org/event/future-fuel-pathways-under-delayed-net-zero-framework) — 19 Dec 2025 - [Workshop on Critical Mineral Processing](https://teriin.org/event/workshop-critical-mineral-processing) — 19 Dec 2025 - [After COP30, What Next? TERI and CRF Decode the Global Climate Agenda](https://teriin.org/event/after-cop30-what-next-teri-and-crf-decode-global-climate-agenda) — 9 De…

## Every question and its suggestions

| Question | Answer | Suggestions (status, scope) | Appeared after |
| --- | --- | --- | --- |
| What does TERI do? | answered | What did COP negotiators identify as areas to build on for climate finance? (*answered, broad*)<br>What strategies did TERI recommend for Goa’s power sector? (*answered, focused*)<br>Who initiated Phase IV of TERI’s Green School Project? (*answered, narrow*) | 4.85 s |
| What research does TERI do? | answered | How does TERI support climate-smart agriculture and improve soil productivity? (*answered, broad*)<br>What approaches does TERI use to make cities more environmentally sustainable? (*answered, broad*)<br>How does TERI turn research into practical solutions? (*partly, broad*, repeat) | 1.9 s |
| What are TERI's latest publications? | answered | — |  |
| What is clean energy? | answered | What clean-energy technologies featured in the India-Japan energy transition workshop? (*answered, focused*)<br>What renewable-energy potential did TERI assess in West Bengal? (*partly, narrow*)<br>How does TERI advance energy-efficient and renewable technologies in India? (*answered, broad*, repeat) | 2.7 s |
| What are green buildings? | answered | How does GRIHA assess a building’s environmental performance? (*answered, focused*)<br>What benchmarks does GRIHA use to assess buildings and habitats? (*answered, focused*, repeat)<br>What role does TERI play in developing India’s Energy Conservation Building Code? (*answered, focused*) | 2.1 s |
| Tell about the director general of TERI | answered | Who was TERI’s first Director General, Rajendra Kumar Pachauri? (*answered, focused*)<br>What does TERI’s 2021–2022 governing council list about Dr Vibha Dhawan? (*answered, narrow*)<br>What is Dr Vibha Dhawan’s academic background and current teaching role? (*partly, focused*, repeat) | 0.74 s |
| What is the mission and vision of TERI? | answered | How does TERI describe its role in energy, environment and sustainable development? (*answered, broad*)<br>Which areas of work does TERI highlight in its brochure? (*answered, broad*)<br>How does TERI connect rural energy solutions with sustainable development? (*answered, broad*) | 1.64 s |
| Can you provide a brief history of The Energy and Resources Institute (TERI)? | answered | Who contributed to TERI’s SDG 12 monitoring appraisal? (*answered, focused*)<br>How does TERI combine research, policy, consultancy, and implementation? (*answered, broad*)<br>What vision guided Darbari S Seth’s work at Tata Chemicals? (*answered, focused*) | 0.41 s |
| Where are TERI's offices located, and how can I contact them? | answered | What is TERI’s headquarters address and contact information? (*answered, narrow*)<br>Where are TERI’s regional centres and campuses besides Bengaluru? (*answered, focused*, repeat)<br>What areas does TERI advance solutions in, according to its 2024–2025 annual report? (*answered, broad*) | 0.0 s |
| Who are TERI's leading experts and researchers? | answered | Which themes did TERI experts address in recommendations for the Union Budget 2023–24? (*answered, focused*)<br>Which campuses and regional centres does TERI operate across India? (*answered, broad*)<br>Which fields did TERI’s Distinguished Fellows Emeritus work in? (*answered, focused*) | 0.33 s |
| What are TERI's ongoing projects? | answered | — |  |
| What are TERI's flagship initiatives and centres of excellence? | answered | What revenue alternatives does TERI suggest for coal-bearing states? (*answered, broad*)<br>What other initiatives does the TERI brochure highlight? (*answered, broad*)<br>Which themes does the CONCOR–TERI Centre of Excellence for Green and Sustainable Logistics address? (*answered, focused*, repeat) | 0.0 s |
| What are the environmental impacts of solar energy? | answered | How can social and environmental assessments involve local communities in solar projects? (*answered, broad*)<br>What environmental assessment was conducted for the Rewa solar project? (*partly, focused*)<br>What land-selection criteria does TERI recommend for solar power plants? (*answered, focused*) | 0.71 s |
| How does climate change affect water availability in India? | answered | What does TERI’s climate modelling examine at regional scales? (*answered, broad*)<br>How does TERI propose strengthening climate resilience in India’s water management? (*answered, broad*)<br>Which regions of India face distinct climate-related water risks? (*answered, focused*, repeat) | 0.0 s |
| What approaches reduce air pollution in Indian cities? | answered | How does TERI’s National Clean Air Action Programme support differ across Indian cities? (*answered, focused*)<br>What elements make an air pollution control strategy holistic and publicly supported? (*answered, broad*)<br>What transport measures have Indian governments used to improve ambient air quality? (*answered, broad*) | 0.38 s |
| Why does sustainable agriculture matter for smallholder farmers? | answered | What watershed approaches can balance agricultural productivity and sustainability? (*answered, broad*)<br>How does TERI define resilience in sustainable agriculture? (*answered, broad*)<br>How could higher minimum support prices encourage crop diversity? (*answered, focused*) | 0.0 s |
| How can buildings be made more energy efficient? | answered | How does TERI define an integrated approach to green buildings? (*answered, broad*, repeat)<br>What building design factors affect the energy performance of glazed facades? (*answered, focused*)<br>How much energy load can retrofitting old buildings reduce? (*answered, narrow*, repeat) | 0.0 s |
| What is CAAQMS? | answered | Where were CAAQMS installed in Mumbai? (*answered, narrow*)<br>How does ambient air quality monitoring support strategic air pollution management? (*answered, broad*)<br>Which pollutants did Maharashtra’s monitoring stations measure? (*answered, broad*, repeat) | 1.54 s |
| GRIHA rating for green buildings | answered | Which environmental impacts do GRIHA projects aim to reduce? (*answered, broad*, repeat)<br>What support does TERI provide for GRIHA certification? (*answered, focused*)<br>How does GRIHA help design and operate resource-efficient buildings? (*answered, broad*, repeat) | 0.03 s |
| What has Dr Leena Srivastava written about? | refusal | — |  |
| Dr Debajit Palit energy access | answered | How does TERI connect energy access with rural livelihoods and development? (*answered, broad*)<br>How can energy access contribute to women’s empowerment in India? (*answered, broad*)<br>What electrification milestones and remaining gaps did Debajit Palit discuss? (*answered, focused*) | 0.78 s |
| Himalayan Adaptation, Water and Resilience research | answered | — |  |
| Which organizations fund TERI's water projects? | answered | What water-related opportunities did TERI and WAPCOS agree to pursue? (*answered, focused*)<br>Which rivers did TERI’s riverbank filtration installations cover? (*answered, narrow*)<br>How did TERI’s riverbank filtration approach demonstrate water-treatment feasibility? (*answered, focused*) | 2.31 s |
| What did the 2019 report say about PM2.5 in Delhi NCR? | answered | How did the Delhi NCR study project future air quality? (*answered, focused*)<br>Which two modelling approaches assessed Delhi NCR’s PM2.5 and PM10 sources? (*answered, focused*)<br>What factors contributed to Delhi’s 22% decline in PM2.5 emissions by 2019? (*answered, narrow*) | 2.63 s |
| SDG 7 progress on energy access in rural India | answered | How many Indian households lacked electricity in 2011, and how many people lacked modern cooking fuels? (*answered, narrow*, repeat)<br>Which rural development initiatives does TERI list under its Energy Access projects? (*answered, focused*)<br>How does TERI link sustainable energy with gender-inclusive rural development? (*answered, broad*, repeat) | 2.95 s |
| How many reports did TERI publish in 2024? | answered | — |  |
| How many news items mention climate change? | answered | — |  |
| How many events did TERI hold in 2025? | answered | List them (*partly, broad*)<br>Break them down by theme (*answered, narrow*) | 0.0 s |
| hi | answered | — |  |
| What is the capital of France? | refusal | — |  |
