/**
 * Sổ Thu Chi ↔ Google Sheet "Investment 2026" — đẩy tình hình tài chính sang app mỗi ngày.
 *
 * CÀI 1 LẦN:
 *  1. Trong sheet: Extensions → Apps Script, xoá code mẫu, dán toàn bộ file này.
 *  2. Trong app Thu Chi: Settings (bánh răng) → "Sao chép token (Sheet sync)".
 *  3. Dán token vào dòng TOKEN bên dưới, bấm Save (Ctrl+S), chọn hàm `setup` ở thanh trên rồi bấm Run.
 *     Google sẽ hỏi quyền (đọc sheet + gọi URL ngoài) → Cho phép.
 *     `setup` lưu token vào Script Properties, tạo lịch chạy 6h sáng hằng ngày, rồi chạy sync ngay lần đầu.
 *  4. Xoá token khỏi dòng TOKEN (đã lưu an toàn trong Properties), Save lại.
 *
 * Mỗi ngày script đọc tab Plan và gửi: tiết kiệm, tiền mặt, đầu tư, thẻ tín dụng tháng này, lương.
 * Không đụng các ô bạn tự điền trong app: mục tiêu, hạn, quỹ khẩn cấp, ngày dự sinh, dự toán sinh.
 */

const APP_URL = 'https://thuchi-production-1d0c.up.railway.app';
const TOKEN = '';          // dán token vào đây, chạy setup() một lần, rồi xoá
const PLAN_SHEET = 'Plan';

function setup() {
  if (TOKEN) PropertiesService.getScriptProperties().setProperty('TC_TOKEN', TOKEN);
  if (!PropertiesService.getScriptProperties().getProperty('TC_TOKEN')) throw new Error('Chưa có token: dán vào dòng TOKEN rồi chạy lại setup()');
  // Xoá trigger cũ (nếu chạy setup lần 2) rồi tạo lịch 6h sáng
  ScriptApp.getProjectTriggers().forEach(t => { if (t.getHandlerFunction() === 'syncFinance') ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('syncFinance').timeBased().everyDays(1).atHour(6).create();
  syncFinance();
}

// Đọc số tiền từ ô: "342.000.000", "1.450.000 ₫", -7316824 … → số nguyên (VND)
function money(v) {
  if (v === null || v === undefined || v === '') return 0;
  if (typeof v === 'number') return Math.round(v);
  const n = String(v).replace(/[^\d\-]/g, '');
  return n ? parseInt(n, 10) : 0;
}

function readPlan() {
  const sh = SpreadsheetApp.getActive().getSheetByName(PLAN_SHEET);
  if (!sh) throw new Error('Không thấy tab ' + PLAN_SHEET);
  const vals = sh.getRange(1, 1, sh.getLastRow(), 8).getValues();   // A..H

  // Khối trên: tìm theo nhãn cột A để không lệch khi bạn chèn dòng
  const byLabel = {};
  vals.forEach(r => { const k = String(r[0] || '').trim().toLowerCase(); if (k) byLabel[k] = r; });
  const savings = money((byLabel['savings'] || [])[1]);
  const cash    = money((byLabel['cash'] || [])[1]);
  const having  = money((byLabel['having'] || [])[1]);
  // Đầu tư = tổng tài sản − tiết kiệm − tiền mặt (gồm cổ phiếu hiện tại + tiền đưa anh)
  const invest  = having > 0 ? having - savings - cash : money((byLabel['đưa anh invest'] || [])[1]);

  // Dòng tháng hiện tại: "saving tháng 10" (hoặc "saving tháng 1.2026")
  const now = new Date();
  const m = now.getMonth() + 1, y = now.getFullYear();
  const re = new RegExp('^saving\\s+tháng\\s+' + m + '(\\.' + y + ')?\\s*$', 'i');
  let row = null;
  for (let i = vals.length - 1; i >= 0; i--) { if (re.test(String(vals[i][0] || '').trim())) { row = vals[i]; break; } }
  const credit_due = row ? Math.abs(money(row[2])) : 0;   // cột C: Credit (prev month), âm trong sheet
  // Lương: cột F của dòng tháng hiện tại; nếu trống lấy cột F dương gần nhất phía trên
  let salary = row ? money(row[5]) : 0;
  if (!salary) {
    for (let i = vals.length - 1; i >= 0; i--) { const e = String(vals[i][4] || '').toLowerCase(); if (e.indexOf('lương') >= 0 && money(vals[i][5]) > 0) { salary = money(vals[i][5]); break; } }
  }
  return { savings, cash, invest, credit_due, salary, net_sheet: having,
           updated: Utilities.formatDate(now, 'Asia/Ho_Chi_Minh', 'yyyy-MM-dd'), source: 'sheet' };
}

function syncFinance() {
  const token = PropertiesService.getScriptProperties().getProperty('TC_TOKEN');
  if (!token) throw new Error('Chưa có token, chạy setup()');
  const finance = readPlan();
  const res = UrlFetchApp.fetch(APP_URL + '/api/settings', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token },
    payload: JSON.stringify({ finance: finance }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  Logger.log('sync %s → HTTP %s: %s', JSON.stringify(finance), code, res.getContentText().slice(0, 200));
  if (code !== 200) throw new Error('App trả ' + code + ': ' + res.getContentText().slice(0, 200));
}

// Chạy tay để xem script đọc ra gì, không gửi đi
function preview() {
  Logger.log(JSON.stringify(readPlan(), null, 2));
}
