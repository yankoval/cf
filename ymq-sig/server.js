// Optional portable HTTP adapter. Uses the same environment as the cloud handler.
const http = require('node:http');
const { handler } = require('./index');
http.createServer(async (request, response) => {
  let body = '';
  for await (const chunk of request) {
    body += chunk;
    if (Buffer.byteLength(body) > 1024 * 1024) {
      response.writeHead(413); response.end(); return;
    }
  }
  try {
    const result = await handler({ httpMethod: request.method, headers: request.headers, body });
    response.writeHead(result.statusCode, result.headers); response.end(result.body);
  } catch {
    response.writeHead(500, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: 'Internal error' }));
  }
}).listen(Number(process.env.PORT) || 8080, process.env.HOST || '127.0.0.1');
