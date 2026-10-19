// Decode only the first JSONL record; do not materialize the entire history.
const fs = require('node:fs');
const zlib = require('node:zlib');
const LIMIT = 1024 * 1024;
function header(file) {
  return new Promise(resolve => {
    if (typeof zlib.createZstdDecompress !== 'function') {
      resolve({file, error: 'Node runtime has no Zstandard decoder'}); return;
    }
    const input = fs.createReadStream(file, {highWaterMark: 16384});
    const decoder = zlib.createZstdDecompress();
    let buffer = Buffer.alloc(0), done = false;
    const finish = result => {
      if (done) return;
      done = true; input.destroy(); decoder.destroy(); resolve({file, ...result});
    };
    input.on('error', () => finish({error: 'File read failed'}));
    decoder.on('error', () => finish({error: 'Zstandard decode failed'}));
    decoder.on('data', chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      const end = buffer.indexOf(10);
      if (end >= 0 && end <= LIMIT) {
        try {
          const row = JSON.parse(buffer.subarray(0, end).toString('utf8'));
          const payload = row.payload || {};
          if (row.type !== 'session_meta' || !payload.id) throw Error();
          finish({id: payload.id, cwd: payload.cwd || null});
        } catch { finish({error: 'Invalid session metadata'}); }
      } else if (buffer.length > LIMIT) finish({error: 'Metadata exceeds size limit'});
    });
    decoder.on('end', () => finish({error: 'Missing first metadata line'}));
    input.pipe(decoder);
  });
}
(async () => {
  const files = JSON.parse(fs.readFileSync(0, 'utf8'));
  for (const file of files) process.stdout.write(JSON.stringify(await header(file)) + '\n');
})().catch(() => { process.exitCode = 1; });
