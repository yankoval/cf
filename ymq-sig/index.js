const SQS = require("@aws-sdk/client-sqs");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const { NodeHttpHandler } = require("@smithy/node-http-handler");
const { timingSafeEqual } = require('node:crypto');
const { createStorageAPI, ACTIONS } = require('./storage-api');

// Configuration from environment
const S3_ENDPOINT = process.env.S3_ENDPOINT || "https://storage.yandexcloud.net";
const YMQ_ENDPOINT = process.env.YMQ_ENDPOINT || "https://message-queue.api.cloud.yandex.net";
const REGION = process.env.AWS_REGION || "ru-central1";
const ACCESS_KEY_ID = process.env.ACCESS_KEY_ID || process.env.S3_ACCESS_KEY_ID || process.env.AWS_ACCESS_KEY_ID;
const SECRET_ACCESS_KEY = process.env.SECRET_ACCESS_KEY || process.env.S3_SECRET_ACCESS_KEY || process.env.AWS_SECRET_ACCESS_KEY;

const API_KEY = process.env.API_KEY;
const QUEUE_URL = process.env.YMQ_QUEUE_URL;
const UPLOAD_BUCKET = process.env.UPLOAD_BUCKET;
const URL_EXPIRATION = parseInt(process.env.URL_EXPIRATION || "3600");
const CLIENT_TIMEOUT = parseInt(process.env.CLIENT_TIMEOUT || "3000"); // Default 3s

// Shared HTTP handler with timeout
const requestHandler = new NodeHttpHandler({
  connectionTimeout: CLIENT_TIMEOUT,
  socketTimeout: CLIENT_TIMEOUT,
});

// Helper to get client config
const getClientConfig = (endpoint) => {
  const config = {
    endpoint: endpoint,
    region: REGION,
    requestHandler: requestHandler,
    maxAttempts: 2,
  };
  // Add credentials if provided explicitly, otherwise SDK will try Service Account
  if (ACCESS_KEY_ID && SECRET_ACCESS_KEY) {
    config.credentials = {
      accessKeyId: ACCESS_KEY_ID,
      secretAccessKey: SECRET_ACCESS_KEY,
      ...(process.env.AWS_SESSION_TOKEN ? { sessionToken: process.env.AWS_SESSION_TOKEN } : {}),
    };
  }
  return config;
};

const sqsClient = new SQS.SQSClient(getClientConfig(YMQ_ENDPOINT));
const s3Client = new S3Client({ ...getClientConfig(S3_ENDPOINT), forcePathStyle: process.env.S3_FORCE_PATH_STYLE === 'true' });
const storageConfig = { ...getClientConfig(process.env.STORAGE_ENDPOINT || S3_ENDPOINT), forcePathStyle: process.env.S3_FORCE_PATH_STYLE === 'true' };
if (process.env.STORAGE_ACCESS_KEY_ID && process.env.STORAGE_SECRET_ACCESS_KEY) {
  storageConfig.credentials = { accessKeyId: process.env.STORAGE_ACCESS_KEY_ID,
    secretAccessKey: process.env.STORAGE_SECRET_ACCESS_KEY,
    ...(process.env.STORAGE_SESSION_TOKEN ? { sessionToken: process.env.STORAGE_SESSION_TOKEN } : {}) };
}
const storageAPI = createStorageAPI({ client: new S3Client(storageConfig), sign: getSignedUrl, env: process.env });

/**
 * Extracts S3 bucket and key from various message formats.
 * Supports:
 * 1. Standard Yandex S3 Event (message.details)
 * 2. Flat format { bucket_id, object_id }
 * 3. Celery v2 Protocol (Base64 body, args containing bucket/key)
 */
function extractS3Details(body) {
  try {
    let data = typeof body === "string" ? JSON.parse(body) : body;

    // Format 1: Standard Yandex S3 Event
    if (data.messages && data.messages[0] && data.messages[0].details) {
      const details = data.messages[0].details;
      return { bucket: details.bucket_id, key: details.object_id };
    }

    // Format 2: Flat format
    if (data.bucket_id && data.object_id) {
      return { bucket: data.bucket_id, key: data.object_id };
    }

    // Format 3: Celery v2 Protocol
    if (data.properties && data.properties.body_encoding === "base64" && data.body) {
      const decodedBody = Buffer.from(data.body, "base64").toString();
      const celeryData = JSON.parse(decodedBody);
      // Celery tasks args are typically [args, kwargs, embed]
      // In this project, args[0] is often an object with bucket/key or a list of such objects
      if (Array.isArray(celeryData) && celeryData[0]) {
        const args = celeryData[0];
        const taskObj = Array.isArray(args) ? args[0] : args;

        // Handle both 'bucket'/'key' and 'bucket_id'/'object_id' naming
        const bucket = taskObj.bucket || taskObj.bucket_id;
        const key = taskObj.key || taskObj.object_id;

        if (bucket && key) {
          return { bucket, key };
        }
      }
    }
  } catch (e) {
    console.warn("Failed to parse message body for S3 details:", e.message);
  }
  return null;
}

module.exports.handler = async function (event, context) {
  const started = Date.now();

  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Api-Key",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
    "Content-Type": "application/json"
  };

  try {
    if (event.httpMethod === "OPTIONS") {
      return { statusCode: 204, headers, isBase64Encoded: false, body: "" };
    }

    const requestHeaders = Object.fromEntries(Object.entries(event.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    const supplied = Buffer.from(String(requestHeaders['x-api-key'] || ''));
    const expected = Buffer.from(API_KEY || '');
    if (!API_KEY) return { statusCode: 503, headers, body: JSON.stringify({ error: 'API authentication is not configured' }) };
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      return { statusCode: 403, headers, body: JSON.stringify({ error: 'Forbidden: Invalid API Key' }) };
    }
    if (event.httpMethod === "GET") {
      return {
        statusCode: 200,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({
          status: "OK",
          authMode: (ACCESS_KEY_ID && SECRET_ACCESS_KEY) ? "Explicit Keys" : "Service Account"
        })
      };
    }

    if (event.httpMethod !== 'POST') return { statusCode: 405, headers, body: JSON.stringify({ error: 'Use POST' }) };

    let body;
    try {
      body = event.body ? JSON.parse(event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString() : event.body) : {};
      if (!body || typeof body !== 'object' || Array.isArray(body) || (body.params !== undefined && (!body.params || typeof body.params !== 'object' || Array.isArray(body.params)))) throw new Error('Invalid body');
    } catch (e) {
      return {
        statusCode: 400,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: "Invalid JSON in request body" })
      };
    }

    const { action, params = {} } = body;

    if (action === 'capabilities') return { statusCode: 200, headers, body: JSON.stringify({ apiVersion: 2, actions: ACTIONS, protocol: 'SignJS', pageSize: 20 }) };
    if (ACTIONS.includes(action)) {
      const response = await storageAPI(action, params);
      return { statusCode: 200, headers, body: JSON.stringify(response) };
    }

    if (action === "ping") {
      return { statusCode: 200, headers, isBase64Encoded: false, body: JSON.stringify({ pong: true }) };
    }

    if (!action) {
      return {
        statusCode: 400,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: "Missing 'action' in request body" })
      };
    }

    if (!params.QueueUrl && QUEUE_URL) {
      params.QueueUrl = QUEUE_URL;
    }
    if (process.env.DISABLE_QUEUE === 'true') return { statusCode: 403, headers, body: JSON.stringify({ error: 'Queue operations disabled in storage test environment' }) };

    const commandName = `${action}Command`;
    if (!SQS[commandName]) {
      return {
        statusCode: 400,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: `Unsupported SQS action: ${action}` })
      };
    }

    console.log(`Executing SQS action: ${action}`);
    const command = new SQS[commandName](params);
    let response = await sqsClient.send(command);

    if (action === "ReceiveMessage" && response.Messages) {
      for (let message of response.Messages) {
        const s3Details = extractS3Details(message.Body);

        if (s3Details) {
          const { bucket, key } = s3Details;

          try {
            const getCommand = new GetObjectCommand({ Bucket: bucket, Key: key });
            const downloadUrl = await getSignedUrl(s3Client, getCommand, { expiresIn: URL_EXPIRATION });

            const uploadBucket = UPLOAD_BUCKET || bucket;
            const sigKey = key + ".sig";
            const putCommand = new PutObjectCommand({
              Bucket: uploadBucket,
              Key: sigKey,
              ContentType: 'application/octet-stream'
            });
            const uploadUrl = await getSignedUrl(s3Client, putCommand, { expiresIn: URL_EXPIRATION });

            message.S3Links = {
              downloadUrl,
              uploadUrl,
              originalBucket: bucket,
              originalKey: key,
              sigKey: sigKey
            };
          } catch (e) {
            console.error("Error generating signed URLs:", e.message);
          }
        }
      }
    }

    return {
      statusCode: 200,
      headers,
      isBase64Encoded: false,
      body: JSON.stringify(response),
    };

  } catch (error) {
    console.error('API request failed', { code: error.name, durationMs: Date.now() - started });
    return {
      statusCode: error.status || error.$metadata?.httpStatusCode || 500,
      headers,
      isBase64Encoded: false,
      body: JSON.stringify({ error: error.message, code: error.name }),
    };
  }
};
