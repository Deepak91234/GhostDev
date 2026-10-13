// Protected controller-owned harness, mounted read-only outside the task workspace.
// Run candidate code in a Node subprocess WITHIN the restricted container, never on the host.
const { spawnSync } = require('node:child_process');
const workRoot = process.env.GHOSTDEV_TEST_WORKDIR || '/work';
const cases = [[10, 1, 10], [10, 3, 30], [2.5, 4, 10]];
let passed = 0;
for (const [price, quantity, expected] of cases) {
  const snippet = `const {calculateTotal}=require(${JSON.stringify(workRoot+'/cart.js')}); process.stdout.write(JSON.stringify({value:calculateTotal(${price},${quantity})}));`;
  const result = spawnSync(process.execPath, ['-e', snippet], {timeout:3000, maxBuffer:4096, env:{PATH:'/usr/local/bin:/usr/bin:/bin'}});
  let actual;
  try { actual = JSON.parse(String(result.stdout)).value; } catch {}
  const ok = result.status === 0 && actual === expected;
  if(ok) passed++;
  console.log(`${ok?'PASS':'FAIL'} quantity=${quantity}: expected ${expected}; received ${actual}`);
}
console.log(JSON.stringify({harness:'ghostdev-cart-v1',tests:cases.length,passed}));
process.exit(passed===cases.length?0:1);
