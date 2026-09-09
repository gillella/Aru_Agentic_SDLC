/** Offline pinned-source contract check. No server, storage, credentials or network. */
const [source, payloadPath] = Bun.argv.slice(2);
if (!source?.startsWith('/') || !payloadPath?.startsWith('/')) throw new Error('absolute fixture paths required');
const { prepare } = await import(`${source}/apps/server/src/mcp/create.ts`);
const { documentPath } = await import(`${source}/packages/protocol/document-url.ts`);
const payload = await Bun.file(payloadPath).json();
const result = prepare(payload);
if (!result?.input) throw new Error('Draft rejected by actual pinned creation schema/dialect');
if (result.input.brief.settledDecisions.length !== 0) throw new Error('Invented consensus');
if (documentPath('gillella', 'unum-catalog', 'catalog') !== '/documents/gillella/unum-catalog/catalog') throw new Error('Document path contract drift');
console.log('Pinned Chopin creation schema, MDX canonicalization and document URL: 3 offline checks passed.');
