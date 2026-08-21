const SHEET_NAME = 'HME Alert Log';

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents || '{}');
    const expectedSecret = PropertiesService.getScriptProperties().getProperty('WEBHOOK_SECRET') || '';

    if (!expectedSecret || payload.secret !== expectedSecret) {
      return jsonResponse({ ok: false, error: 'Unauthorized' });
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sheet = ss.getSheetByName(SHEET_NAME);
    if (!sheet) {
      sheet = ss.insertSheet(SHEET_NAME);
      sheet.appendRow([
        'Date',
        'Time',
        'Location',
        'HME Current Hour Average (sec)',
        'Threshold (sec)',
        'Required Minutes',
        'Result',
        'OneSignal Notification ID'
      ]);
      sheet.getRange(1, 1, 1, 8).setFontWeight('bold');
      sheet.setFrozenRows(1);
    }

    sheet.appendRow([
      payload.date || '',
      payload.time || '',
      payload.location || '',
      payload.average_seconds || '',
      payload.threshold_seconds || '',
      payload.required_minutes || '',
      payload.result || '',
      payload.onesignal_notification_id || ''
    ]);

    return jsonResponse({ ok: true });
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err) });
  }
}

function jsonResponse(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}
