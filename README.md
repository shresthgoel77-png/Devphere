DevNexus is a dark-mode-first developer intelligence app foundation built with Next.js App Router. It presents static development fixtures for news, AI models, developer builds, and hackathons. The foundation page is implemented in `src/app/page.tsx`.

## DevNexus foundation

### What is DevNexus

DevNexus is a dark-mode-first developer intelligence app foundation for presenting information across news, AI models, developer builds, and hackathons. This foundation reads static published JSON and has no backend.

### Zero-cost constraints

DevNexus is designed to run without paid services, a credit card, authentication, accounts, or a database. The app does not make live-source claims, and this repository makes no deployment claims.

### Local development

Install the locked dependencies, then start the app:

```bash
npm ci
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) to view the app. Run project checks with:

```bash
npm run typecheck
npm run lint
npm run test
npm run build
```

### Data layer

The data layer consists of static JSON files under `public/data/`. Dataset files use the versioned envelope format with `schemaVersion`, `dataOrigin`, `generatedAtIso`, and `items`. Development fixtures are marked with `dataOrigin: "seed"` and visibly labelled in the interface; seed content is not verified live data.

### Current status

This is the Phase 1 foundation. Ingestion has not been implemented, all displayed data is seed data, and no live data or deployment is claimed.
