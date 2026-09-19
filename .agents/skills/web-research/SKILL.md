---
name: web-research
description: Research a topic on the web with web_search then web_fetch the best sources, and synthesize a cited summary.
triggers: research, look up, find information, latest news
---

# Web Research Skill

When asked to research a topic or find current information:

1. **Search first**: call `web_search` with a focused query (add the year or version number if relevant).
2. **Pick 2-3 credible sources** from the results (official docs > reputable blogs > forums).
3. **Fetch them**: call `web_fetch` on each chosen URL to get full text.
4. **Synthesize**: write a concise answer with sections. Cite each claim inline as [source-title].
5. **Never invent URLs.** Only reference pages you actually fetched.
6. If search returns nothing or errors, say so — do not fabricate results.
