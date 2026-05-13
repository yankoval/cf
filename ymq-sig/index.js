const SQS = require("@aws-sdk/client-sqs");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const { NodeHttpHandler } = require("@smithy/node-http-handler");

// Configuration from environment
const S3_ENDPOINT = process.env.S3_ENDPOINT || "https://storage.yandexcloud.net";
const YMQ_ENDPOINT = process.env.YMQ_ENDPOINT || "https://message-queue.api.cloud.yandex.net";
const REGION = process.env.AWS_REGION || "ru-central1";
const ACCESS_KEY_ID = process.env.ACCESS_KEY_ID || process.env.S3_ACCESS_KEY_ID;
const SECRET_ACCESS_KEY = process.env.SECRET_ACCESS_KEY || process.env.S3_SECRET_ACCESS_KEY;

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
  };
  // Add credentials if provided explicitly, otherwise SDK will try Service Account
  if (ACCESS_KEY_ID && SECRET_ACCESS_KEY) {
    config.credentials = {
      accessKeyId: ACCESS_KEY_ID,
      secretAccessKey: SECRET_ACCESS_KEY,
    };
  }
  return config;
};

const sqsClient = new SQS.SQSClient(getClientConfig(YMQ_ENDPOINT));
const s3Client = new S3Client(getClientConfig(S3_ENDPOINT));

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
  console.log("Event received:", JSON.stringify(event));

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

    const requestHeaders = event.headers || {};
    const apiKey = requestHeaders["X-Api-Key"] || requestHeaders["x-api-key"];

    if (API_KEY && apiKey !== API_KEY) {
      return {
        statusCode: 403,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: "Forbidden: Invalid API Key" })
      };
    }

    let body;
    try {
      body = event.body ? JSON.parse(event.body) : {};
    } catch (e) {
      return {
        statusCode: 400,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: "Invalid JSON in request body" })
      };
    }

    const { action, params = {} } = body;

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
          console.log(`Found S3 details: bucket=${bucket}, key=${key}`);

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
    console.error("Execution error:", error);
    return {
      statusCode: error.$metadata?.httpStatusCode || 500,
      headers,
      isBase64Encoded: false,
      body: JSON.stringify({ error: error.message, code: error.name }),
    };
  }
};
