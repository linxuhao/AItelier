import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, realpath, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DSH_PACKAGE, pinnedDshVersion, resolveDshEntry, measuredEntryForTest, withoutCallerMode,
  dshEntryCandidates, installRoot, packageRoot, executionModeKeys } from './dsh-entry.mjs';

const PINNED = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8')).dependencies[DSH_PACKAGE];

/** A throwaway "checkout" whose manifest pins `version`. */
async function makePinRoot(version) {
  const root = await mkdtemp(join(tmpdir(), 'dsh-entry-root-'));
  await writeFile(join(root, 'package.json'), JSON.stringify({ private: true, dependencies: { '@deepseek-ai/dsh': version } }));
  return root;
}

/** An install tree under `directory`, shaped like `npm i @deepseek-ai/dsh@version`. */
async function makeInstall(directory, version) {
  const pkg = join(directory, 'node_modules', DSH_PACKAGE);
  await mkdir(join(pkg, 'lib'), { recursive: true });
  await mkdir(join(directory, 'node_modules', '.bin'), { recursive: true });
  await writeFile(join(pkg, 'package.json'), JSON.stringify({ name: DSH_PACKAGE, version }));
  await writeFile(join(pkg, 'lib', 'bin.js'), '#!/usr/bin/env node\n');
  await symlink(`../${DSH_PACKAGE}/lib/bin.js`, join(directory, 'node_modules', '.bin', 'dsh'));
  return { entry: join(directory, 'node_modules', '.bin', 'dsh'), real: await realpath(join(pkg, 'lib', 'bin.js')) };
}

test('the pin comes from the committed manifest, not from a constant', async () => {
  const committed = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8')).dependencies[DSH_PACKAGE];
  assert.equal(await pinnedDshVersion(), committed);
  assert.match(committed, /^\d+\.\d+\.\d+/);
  const other = await makePinRoot('9.9.9-test');
  assert.equal(await pinnedDshVersion(other), '9.9.9-test', 'the resolver did not read the manifest it was pointed at');
  const unpinned = await mkdtemp(join(tmpdir(), 'dsh-entry-unpinned-'));
  await writeFile(join(unpinned, 'package.json'), JSON.stringify({ private: true, dependencies: {} }));
  await assert.rejects(pinnedDshVersion(unpinned), /does not pin @deepseek-ai\/dsh/);
  await rm(other, { recursive: true }); await rm(unpinned, { recursive: true });
});

test('an installed binary at the pinned version is measured, and the resolver says which one', async () => {
  const root = await makePinRoot(PINNED);
  const install = await mkdtemp(join(tmpdir(), 'dsh-entry-install-'));
  const fixture = await makeInstall(install, PINNED);
  const resolution = await resolveDshEntry({ env: { AITELIER_DSH_TEST_ENTRY: fixture.entry }, home: root, root, systemRoots: false });
  assert.equal(resolution.status, 'measured');
  assert.equal(resolution.version, PINNED);
  assert.equal(resolution.pinned, PINNED);
  assert.equal(resolution.entry, fixture.real, 'the measured entry must be the real binary, not the .bin shim');
  assert.equal(resolution.source, 'AITELIER_DSH_TEST_ENTRY');
  assert.deepEqual(resolution.mismatches, []);
  assert.equal(resolution.reason, null);
  await rm(root, { recursive: true }); await rm(install, { recursive: true });
});

test('a differently-versioned binary is refused instead of measured', async () => {
  const root = await makePinRoot(PINNED);
  const install = await mkdtemp(join(tmpdir(), 'dsh-entry-stale-'));
  const stale = await makeInstall(install, '0.2.0-rc.5');
  const resolution = await resolveDshEntry({ env: { AITELIER_DSH_TEST_ENTRY: stale.entry }, home: root, root, systemRoots: false });
  assert.equal(resolution.status, 'refused', 'a wrong-version binary must never be reported as measured');
  assert.equal(resolution.entry, null);
  assert.equal(resolution.version, null);
  assert.equal(resolution.mismatches.length, 1);
  assert.equal(resolution.mismatches[0].version, '0.2.0-rc.5');
  assert.match(resolution.reason, /0\.2\.0-rc\.5/);
  assert.match(resolution.reason, new RegExp(PINNED.replaceAll('.', '\\.')), 'the refusal must name the version it wanted');
  await rm(root, { recursive: true }); await rm(install, { recursive: true });
});

test('a binary nobody can attribute to the pin is refused, not trusted', async () => {
  const root = await makePinRoot(PINNED);
  const loose = join(root, 'loose');
  await mkdir(loose, { recursive: true });
  await writeFile(join(loose, 'package.json'), JSON.stringify({ name: 'not-dsh' }));
  await writeFile(join(loose, 'dsh'), '#!/usr/bin/env node\n');
  const resolution = await resolveDshEntry({ env: { AITELIER_DSH_TEST_ENTRY: join(loose, 'dsh') }, home: root, root, systemRoots: false });
  assert.equal(resolution.status, 'refused');
  assert.equal(resolution.mismatches[0].version, null);
  assert.match(resolution.reason, /unknown version/);
  await rm(root, { recursive: true });
});

test('a stale override does not displace the pinned checkout install, and the mismatch stays visible', async () => {
  const root = await makePinRoot(PINNED);
  const pinned = await makeInstall(root, PINNED);
  const staleHome = await mkdtemp(join(tmpdir(), 'dsh-entry-stale-'));
  const stale = await makeInstall(staleHome, '0.1.0');
  const resolution = await resolveDshEntry({ env: { AITELIER_DSH_TEST_ENTRY: stale.entry }, home: staleHome, root, systemRoots: false });
  assert.equal(resolution.status, 'measured');
  assert.equal(resolution.source, 'this checkout');
  assert.equal(resolution.entry, pinned.real);
  assert.deepEqual(resolution.mismatches.map(m => m.path), [stale.entry]);
  await rm(root, { recursive: true }); await rm(staleHome, { recursive: true });
});

test('with nothing installed the suite is unmeasured and says where it looked', async () => {
  const root = await makePinRoot(PINNED);
  const empty = await mkdtemp(join(tmpdir(), 'dsh-entry-home-'));
  const resolution = await resolveDshEntry({ env: {}, home: empty, root, systemRoots: false });
  assert.equal(resolution.status, 'unmeasured');
  assert.equal(resolution.entry, null);
  assert.equal(resolution.version, null);
  assert.match(resolution.reason, /no installed @deepseek-ai\/dsh/);
  assert.ok(resolution.searched.includes(join(root, 'node_modules', '.bin', 'dsh')), JSON.stringify(resolution.searched));
  assert.ok(resolution.searched.every(path => path.startsWith(root) || path.startsWith(empty)), JSON.stringify(resolution.searched));
  await rm(root, { recursive: true }); await rm(empty, { recursive: true });
});

test('candidate order is explicit-first and never depends on cwd', async () => {
  const candidates = dshEntryCandidates({ env: { AITELIER_DSH_TEST_ENTRY: '/explicit/dsh', AITELIER_DSH_BIN: '/deploy/dsh' },
    home: '/home/fixture', root: '/checkout' });
  assert.deepEqual(candidates.map(c => c.source), ['AITELIER_DSH_TEST_ENTRY', 'AITELIER_DSH_BIN', 'this checkout', 'host install', 'container install']);
  assert.equal(candidates[2].path, '/checkout/node_modules/.bin/dsh');
  assert.equal(candidates[3].path, '/home/fixture/.local/share/aitelier-dsh/node_modules/.bin/dsh');
  assert.equal(installRoot({ env: { AITELIER_DSH_HOME: '/opt/custom' } }), '/opt/custom');
  const relative = dshEntryCandidates({ env: { AITELIER_DSH_TEST_ENTRY: 'node_modules/.bin/dsh' }, root: '/checkout', systemRoots: false });
  assert.equal(relative[0].path, join(process.cwd(), 'node_modules', '.bin', 'dsh'), 'a relative override must be resolved, not leaked');
});

test('withoutCallerMode drops exactly the mode the invoking shell decided', () => {
  const caller = { AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access', AITELIER_DSH_ENTRY: '/entry', PATH: '/bin' };
  const environment = withoutCallerMode(caller);
  assert.deepEqual(executionModeKeys, ['AITELIER_DSH_EXECUTION', 'DSH_PERMISSION_MODE']);
  for (const key of executionModeKeys) assert.ok(!Object.hasOwn(environment, key), `${key} survived into the driver environment`);
  assert.equal(environment.AITELIER_DSH_ENTRY, '/entry');
  assert.equal(environment.PATH, '/bin');
  assert.equal(caller.AITELIER_DSH_EXECUTION, 'host', 'the caller environment was mutated');
});

test('measuredEntryForTest gives every pinned-DSH site one honest shape', async () => {
  const root = await makePinRoot(PINNED);
  const install = await mkdtemp(join(tmpdir(), 'dsh-entry-install-'));
  const fixture = await makeInstall(install, PINNED);
  const seen = [];
  const context = { skip: reason => seen.push(['skip', reason]), diagnostic: message => seen.push(['diagnostic', message]) };
  assert.equal(await measuredEntryForTest(context, { env: { AITELIER_DSH_TEST_ENTRY: fixture.entry }, home: install, root, systemRoots: false }), fixture.real);
  assert.match(seen.at(-1)[1], new RegExp(`measuring @deepseek-ai/dsh ${PINNED.replaceAll('.', '\\.')}`));

  const empty = await mkdtemp(join(tmpdir(), 'dsh-entry-home-'));
  const skips = [];
  assert.equal(await measuredEntryForTest({ skip: reason => skips.push(reason), diagnostic: () => {} },
    { env: {}, home: empty, root, systemRoots: false }), null);
  assert.equal(skips.length, 1);
  assert.match(skips[0], /no installed @deepseek-ai\/dsh/);

  const staleHome = await mkdtemp(join(tmpdir(), 'dsh-entry-stale-'));
  const stale = await makeInstall(staleHome, '0.1.0');
  const refusals = [];
  await assert.rejects(measuredEntryForTest({ skip: reason => refusals.push(reason), diagnostic: () => {} },
    { env: { AITELIER_DSH_TEST_ENTRY: stale.entry }, home: empty, root, systemRoots: false }), /refusing to measure @deepseek-ai\/dsh/);
  assert.deepEqual(refusals, [], 'a refusal must fail loudly, never be reported as an unmeasured skip');
  await rm(root, { recursive: true }); await rm(install, { recursive: true });
  await rm(empty, { recursive: true }); await rm(staleHome, { recursive: true });
});
