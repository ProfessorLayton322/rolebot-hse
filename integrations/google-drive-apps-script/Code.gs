/**
 * Rolebot's narrowly scoped Google Drive upload gateway.
 *
 * Deploy this project as a web app that executes as the deploying user. AWS
 * sends an HMAC-signed JSON request; no Google credential leaves this project.
 *
 * Required script properties:
 *   ROLEBOT_FOLDER_ID
 *   ROLEBOT_HMAC_SECRET
 *
 * The Advanced Drive service (Drive API v3) must be enabled for the project.
 */

const ROLEBOT_CONFIG = Object.freeze({
  schemaVersion: 1,
  maxClockSkewSeconds: 300,
  maxXlsxBytes: 10 * 1024 * 1024,
  maxRequestCharacters: 15 * 1024 * 1024,
  xlsxMimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  emptySha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  allowedOperations: Object.freeze(["health", "exists_xlsx", "upsert_xlsx", "trash_xlsx"]),
  allowedNamePrefixes: Object.freeze(["events — ", "passes — "]),
});

function doGet() {
  return jsonResponse_({
    ok: false,
    error: "method_not_allowed",
    message: "Use a signed POST request.",
  });
}

function doPost(event) {
  let requestId = "unknown";
  let operation = "unknown";

  try {
    const rawBody = requireRequestBody_(event);
    const request = parseRequest_(rawBody);
    requestId = request.request_id;
    operation = request.operation;

    const settings = loadSettings_();
    authenticateRequest_(request, settings.hmacSecret);
    const contentBytes = validateContent_(request);

    if (request.operation === "health") {
      return jsonResponse_(health_(settings.folderId, request.request_id));
    }

    const lock = LockService.getScriptLock();
    if (!lock.tryLock(30000)) {
      fail_("busy", "Another Drive operation is still running; retry shortly.");
    }

    try {
      let result;
      if (request.operation === "exists_xlsx") {
        result = existsXlsx_(settings.folderId, request.name);
      } else if (request.operation === "upsert_xlsx") {
        result = upsertXlsx_(settings.folderId, request.name, contentBytes);
      } else if (request.operation === "trash_xlsx") {
        result = trashXlsx_(settings.folderId, request.name);
      } else {
        fail_("invalid_operation", "Unsupported operation.");
      }

      result.ok = true;
      result.operation = request.operation;
      result.request_id = request.request_id;
      return jsonResponse_(result);
    } finally {
      lock.releaseLock();
    }
  } catch (error) {
    const safeError = normalizeError_(error);
    console.error(
      JSON.stringify({
        request_id: requestId,
        operation: operation,
        error: safeError.code,
        message: safeError.message,
      }),
    );
    return jsonResponse_({
      ok: false,
      request_id: requestId,
      error: safeError.code,
      message: safeError.message,
    });
  }
}

function requireRequestBody_(event) {
  if (!event || !event.postData || typeof event.postData.contents !== "string") {
    fail_("invalid_request", "A JSON request body is required.");
  }
  if (event.postData.contents.length > ROLEBOT_CONFIG.maxRequestCharacters) {
    fail_("request_too_large", "The request body is too large.");
  }
  return event.postData.contents;
}

function parseRequest_(rawBody) {
  let request;
  try {
    request = JSON.parse(rawBody);
  } catch (error) {
    fail_("invalid_json", "The request body is not valid JSON.");
  }

  if (!request || Array.isArray(request) || typeof request !== "object") {
    fail_("invalid_request", "The JSON body must be an object.");
  }

  const allowedKeys = [
    "schema_version",
    "timestamp",
    "request_id",
    "operation",
    "name",
    "content_sha256",
    "content_base64",
    "signature",
  ];
  const requestKeys = Object.keys(request);
  if (
    requestKeys.length !== allowedKeys.length ||
    requestKeys.some(function (key) {
      return allowedKeys.indexOf(key) === -1;
    })
  ) {
    fail_("invalid_request", "The request has missing or unexpected fields.");
  }

  if (request.schema_version !== ROLEBOT_CONFIG.schemaVersion) {
    fail_("invalid_schema", "Unsupported request schema version.");
  }
  if (!Number.isSafeInteger(request.timestamp)) {
    fail_("invalid_timestamp", "timestamp must be an integer Unix time.");
  }
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - request.timestamp) > ROLEBOT_CONFIG.maxClockSkewSeconds) {
    fail_("stale_request", "The request timestamp is outside the five-minute window.");
  }
  if (
    typeof request.request_id !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(request.request_id)
  ) {
    fail_("invalid_request_id", "request_id must be a UUID.");
  }
  if (
    typeof request.operation !== "string" ||
    ROLEBOT_CONFIG.allowedOperations.indexOf(request.operation) === -1
  ) {
    fail_("invalid_operation", "Unsupported operation.");
  }
  if (typeof request.name !== "string") {
    fail_("invalid_name", "name must be a string.");
  }
  if (typeof request.content_base64 !== "string") {
    fail_("invalid_content", "content_base64 must be a string.");
  }
  if (
    typeof request.content_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(request.content_sha256)
  ) {
    fail_("invalid_content_hash", "content_sha256 must be a lowercase SHA-256 hex digest.");
  }
  if (typeof request.signature !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(request.signature)) {
    fail_("invalid_signature", "signature must be an unpadded base64url HMAC-SHA-256 value.");
  }

  if (request.operation === "health") {
    if (
      request.name !== "" ||
      request.content_base64 !== "" ||
      request.content_sha256 !== ROLEBOT_CONFIG.emptySha256
    ) {
      fail_("invalid_health_request", "A health request must not contain a name or content.");
    }
  } else {
    validateXlsxName_(request.name);
  }

  if (
    request.operation !== "upsert_xlsx" &&
    (request.content_base64 !== "" || request.content_sha256 !== ROLEBOT_CONFIG.emptySha256)
  ) {
    fail_("unexpected_content", "Only upsert_xlsx accepts file content.");
  }

  return request;
}

function loadSettings_() {
  const properties = PropertiesService.getScriptProperties();
  const folderId = properties.getProperty("ROLEBOT_FOLDER_ID");
  const hmacSecret = properties.getProperty("ROLEBOT_HMAC_SECRET");

  if (!folderId || !/^[A-Za-z0-9_-]{10,}$/.test(folderId)) {
    fail_("configuration_error", "ROLEBOT_FOLDER_ID is missing or invalid.");
  }
  if (!hmacSecret || hmacSecret.length < 32) {
    fail_("configuration_error", "ROLEBOT_HMAC_SECRET must contain at least 32 characters.");
  }
  return { folderId: folderId, hmacSecret: hmacSecret };
}

function authenticateRequest_(request, hmacSecret) {
  const canonical = [
    String(request.schema_version),
    String(request.timestamp),
    request.request_id,
    request.operation,
    request.name,
    request.content_sha256,
  ].join("\n");
  const expected = Utilities.computeHmacSha256Signature(
    canonical,
    hmacSecret,
    Utilities.Charset.UTF_8,
  );

  let supplied;
  try {
    supplied = Utilities.base64DecodeWebSafe(request.signature);
  } catch (error) {
    fail_("invalid_signature", "The request signature is invalid.");
  }
  if (!constantTimeBytesEqual_(expected, supplied)) {
    fail_("authentication_failed", "The request signature is invalid.");
  }
}

function validateContent_(request) {
  if (request.operation !== "upsert_xlsx") {
    return [];
  }

  const maximumBase64Length = Math.ceil(ROLEBOT_CONFIG.maxXlsxBytes / 3) * 4;
  if (request.content_base64.length > maximumBase64Length) {
    fail_("file_too_large", "The XLSX file exceeds the 10 MiB limit.");
  }

  let contentBytes;
  try {
    contentBytes = Utilities.base64Decode(request.content_base64);
  } catch (error) {
    fail_("invalid_content", "content_base64 is not valid base64.");
  }
  if (contentBytes.length === 0) {
    fail_("invalid_content", "The XLSX file must not be empty.");
  }
  if (contentBytes.length > ROLEBOT_CONFIG.maxXlsxBytes) {
    fail_("file_too_large", "The XLSX file exceeds the 10 MiB limit.");
  }

  const actualHash = bytesToHex_(
    Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, contentBytes),
  );
  if (actualHash !== request.content_sha256) {
    fail_("content_hash_mismatch", "The decoded content does not match content_sha256.");
  }
  return contentBytes;
}

function validateXlsxName_(name) {
  const hasAllowedPrefix = ROLEBOT_CONFIG.allowedNamePrefixes.some(function (prefix) {
    return name.indexOf(prefix) === 0;
  });
  if (!hasAllowedPrefix || !/\.xlsx$/i.test(name)) {
    fail_("invalid_name", "Only rolebot event and pass XLSX names are allowed.");
  }
  if (/[\\/\u0000-\u001f\u007f]/.test(name)) {
    fail_("invalid_name", "The file name contains a forbidden character.");
  }
  if (Utilities.newBlob(name).getBytes().length > 240) {
    fail_("invalid_name", "The UTF-8 file name is too long.");
  }
}

function health_(folderId, requestId) {
  const folder = Drive.Files.get(folderId, {
    supportsAllDrives: true,
    fields: "id,name,mimeType,capabilities(canAddChildren)",
  });
  if (folder.mimeType !== "application/vnd.google-apps.folder") {
    fail_("configuration_error", "ROLEBOT_FOLDER_ID does not identify a folder.");
  }
  if (!folder.capabilities || folder.capabilities.canAddChildren !== true) {
    fail_("configuration_error", "The deploying user cannot add files to the configured folder.");
  }
  return {
    ok: true,
    operation: "health",
    request_id: requestId,
    folder_id: folder.id,
    folder_name: folder.name,
  };
}

function existsXlsx_(folderId, name) {
  const file = findExactlyOneFile_(folderId, name);
  if (!file) {
    return { exists: false, name: name };
  }
  requireXlsxMimeType_(file);
  return fileResult_(file, true);
}

function upsertXlsx_(folderId, name, contentBytes) {
  const blob = Utilities.newBlob(contentBytes, ROLEBOT_CONFIG.xlsxMimeType, name);
  let file = findExactlyOneFile_(folderId, name);
  let created = false;

  if (file) {
    requireXlsxMimeType_(file);
    file = Drive.Files.update({}, file.id, blob, {
      supportsAllDrives: true,
      fields: "id,name,mimeType,webViewLink",
    });
  } else {
    file = Drive.Files.create(
      {
        name: name,
        mimeType: ROLEBOT_CONFIG.xlsxMimeType,
        parents: [folderId],
      },
      blob,
      {
        supportsAllDrives: true,
        fields: "id,name,mimeType,webViewLink",
      },
    );
    created = true;
  }

  ensureAnyoneWithLinkCanRead_(file.id);
  const result = fileResult_(file, true);
  result.created = created;
  return result;
}

function trashXlsx_(folderId, name) {
  const file = findExactlyOneFile_(folderId, name);
  if (!file) {
    return { existed: false, name: name };
  }
  requireXlsxMimeType_(file);
  DriveApp.getFileById(file.id).setTrashed(true);
  return {
    existed: true,
    name: file.name,
    file_id: file.id,
  };
}

function findExactlyOneFile_(folderId, name) {
  const escapedFolderId = escapeDriveQueryValue_(folderId);
  const escapedName = escapeDriveQueryValue_(name);
  const response = Drive.Files.list({
    q:
      "'" +
      escapedFolderId +
      "' in parents and name = '" +
      escapedName +
      "' and trashed = false",
    spaces: "drive",
    pageSize: 2,
    includeItemsFromAllDrives: true,
    supportsAllDrives: true,
    fields: "files(id,name,mimeType,webViewLink)",
  });
  const files = response.files || [];
  if (files.length > 1) {
    fail_("duplicate_name", "More than one file has the requested name in the target folder.");
  }
  return files.length === 1 ? files[0] : null;
}

function ensureAnyoneWithLinkCanRead_(fileId) {
  const response = Drive.Permissions.list(fileId, {
    supportsAllDrives: true,
    fields: "permissions(type,role)",
  });
  const alreadyPublic = (response.permissions || []).some(function (permission) {
    return permission.type === "anyone" && permission.role === "reader";
  });
  if (!alreadyPublic) {
    Drive.Permissions.create(
      { type: "anyone", role: "reader", allowFileDiscovery: false },
      fileId,
      { supportsAllDrives: true, fields: "id" },
    );
  }
}

function requireXlsxMimeType_(file) {
  if (file.mimeType !== ROLEBOT_CONFIG.xlsxMimeType) {
    fail_("wrong_file_type", "A non-XLSX file already uses the requested name.");
  }
}

function fileResult_(file, exists) {
  return {
    exists: exists,
    name: file.name,
    file_id: file.id,
    url: file.webViewLink || "https://drive.google.com/file/d/" + encodeURIComponent(file.id) + "/view",
  };
}

function escapeDriveQueryValue_(value) {
  return value.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

function constantTimeBytesEqual_(left, right) {
  if (!left || !right) {
    return false;
  }
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const leftByte = index < left.length ? left[index] & 255 : 0;
    const rightByte = index < right.length ? right[index] & 255 : 0;
    difference |= leftByte ^ rightByte;
  }
  return difference === 0;
}

function bytesToHex_(bytes) {
  return bytes
    .map(function (value) {
      return ("0" + (value & 255).toString(16)).slice(-2);
    })
    .join("");
}

function jsonResponse_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(
    ContentService.MimeType.JSON,
  );
}

function fail_(code, message) {
  const error = new Error(message);
  error.rolebotCode = code;
  throw error;
}

function normalizeError_(error) {
  if (error && error.rolebotCode) {
    return { code: error.rolebotCode, message: error.message };
  }
  return {
    code: "drive_error",
    message: "Google Drive could not complete the operation.",
  };
}
