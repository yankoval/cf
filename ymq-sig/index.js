const SQS = require("@aws-sdk/client-sqs");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");

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

// Helper to get client config
const getClientConfig = (endpoint) => {
  const config = {
    endpoint: endpoint,
    region: REGION,
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

module.exports.handler = async function (event, context) {
  console.log("Event received:", JSON.stringify(event));

  // CORS Headers
  const headers = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Api-Key",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
    "Content-Type": "application/json"
  };

  try {
    // Handle preflight
    if (event.httpMethod === "OPTIONS") {
      return {
        statusCode: 204,
        headers,
        isBase64Encoded: false,
        body: ""
      };
    }

    // Health check
    if (event.httpMethod === "GET") {
      return {
        statusCode: 200,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({
          status: "OK",
          message: "YMQ Proxy is running",
          authMode: (ACCESS_KEY_ID && SECRET_ACCESS_KEY) ? "Explicit Keys" : "Service Account"
        })
      };
    }

    // API Key Validation
    const requestHeaders = event.headers || {};
    const apiKey = requestHeaders["X-Api-Key"] || requestHeaders["x-api-key"];

    if (API_KEY && apiKey !== API_KEY) {
      console.warn("Invalid API Key provided");
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
      console.error("JSON Parse Error:", e.message);
      return {
        statusCode: 400,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: "Invalid JSON in request body" })
      };
    }

    const { action, params = {} } = body;

    // Support a simple ping action
    if (action === "ping") {
      return {
        statusCode: 200,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ pong: true })
      };
    }

    if (!action) {
      return {
        statusCode: 400,
        headers,
        isBase64Encoded: false,
        body: JSON.stringify({ error: "Missing 'action' in request body" })
      };
    }

    // Default to configured QueueUrl if not provided in params
    if (!params.QueueUrl && QUEUE_URL) {
      params.QueueUrl = QUEUE_URL;
    }

    // Dynamic Command execution for SQS
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

    // Enrichment for ReceiveMessage: Generate signed S3 links
    if (action === "ReceiveMessage" && response.Messages) {
      for (let message of response.Messages) {
        try {
          const bodyData = JSON.parse(message.Body);
          let s3Event = null;

          if (bodyData.messages && bodyData.messages[0] && bodyData.messages[0].details) {
            s3Event = bodyData.messages[0].details;
          } else if (bodyData.bucket_id && bodyData.object_id) {
            s3Event = bodyData;
          }

          if (s3Event) {
            const bucket = s3Event.bucket_id;
            const key = s3Event.object_id;

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
          }
        } catch (e) {
          console.warn("Could enrich message:", e.message);
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
      body: JSON.stringify({
        error: error.message,
        code: error.name
      }),
    };
  }
};
