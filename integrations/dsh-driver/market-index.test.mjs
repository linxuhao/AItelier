/** The plugin market index, market/index.json at the repository root (served from its raw GitHub
 *  URL; integrations/dsh-director/README.md tells users to add that URL): DSH's own validator, from
 *  the pinned install, accepts it, and what it lists is the plugin package as this checkout packs
 *  it. The validator cannot check that (its README: generator obligations are unchecked), so this
 *  file does. A publishTimestamp of 0 is the placeholder the publish step replaces with the
 *  registry's time (the plugin README's "Releasing the package"). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolveDshLibraries } from './dsh-entry.mjs';

const read = path => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const SERVED_AT = 'https://raw.githubusercontent.com/linxuhao/AItelier/main/market/index.json';

const libraries = await resolveDshLibraries();
if (libraries.status === 'refused') throw new Error(libraries.reason);

if (libraries.status === 'unmeasured') {
  test('market/index.json against DSH\'s validator (needs a pinned @deepseek-ai/dsh install to import from)', t => t.skip(libraries.reason));
} else {
  const { marketInstallTarget, parseMarketIndex } = await import(libraries.require.resolve('@deepseek-ai/dsh-plugin-market-index'));
  const { default: semver } = await import(libraries.require.resolve('semver'));
  const pkg = read('../dsh-director/package.json');

  test('market/index.json: DSH validates it at the URL it is served from', () => {
    const index = parseMarketIndex(read('../../market/index.json'), SERVED_AT);
    assert.deepEqual(index.plugins.map(plugin => plugin.name), [pkg.name]);
    for (const text of [index.title, index.description, index.plugins[0].title, index.plugins[0].description])
      assert.deepEqual(Object.keys(text).sort(), ['en', 'zh']);
  });

  test('market/index.json lists the plugin package as this checkout packs it, installable on the tested DSH', () => {
    const [entry] = parseMarketIndex(read('../../market/index.json'), SERVED_AT).plugins;
    const version = entry.versions.find(v => v.version === pkg.version);
    assert.ok(version, `market/index.json does not list ${pkg.name}@${pkg.version}`);
    assert.deepEqual(version.source, { type: 'npm' });
    assert.deepEqual(marketInstallTarget(entry.name, version), { spec: `${pkg.name}@${pkg.version}` });
    assert.equal(version.engines.dsh, pkg.engines.dsh, 'the index and package.json name one DSH range');
    // DSH skips a bundle whose engines.dsh its own version does not satisfy (prereleases included).
    assert.ok(semver.satisfies(libraries.version, pkg.engines.dsh, { includePrerelease: true }),
      `the tested DSH ${libraries.version} is outside ${pkg.engines.dsh}`);
    assert.deepEqual(pkg.dsh, { bundle: { patch: './cordis.patch.yml' } });
    assert.deepEqual(entry.repository, { url: pkg.repository.url.replace(/^git\+/, '').replace(/\.git$/, ''),
      directory: pkg.repository.directory });
    assert.equal(entry.homepage, pkg.homepage);
    assert.ok(Number.isInteger(version.publishTimestamp) && version.publishTimestamp >= 0);
  });
}
