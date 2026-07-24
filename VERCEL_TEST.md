# Vercel 部署測試指南

## 🚀 簡化版本說明

由於原版應用過於複雜（包含大量依賴項和SocketIO），我們創建了一個簡化的測試版本來驗證Vercel部署是否正常工作。

## 📋 當前配置

### 文件結構
```
├── api/index.py          # 簡化的Flask應用
├── vercel.json           # Vercel配置
├── requirements_vercel.txt # 最小依賴項（只有Flask）
└── .vercelignore         # 忽略不必要的文件
```

### 依賴項
- `Flask==3.0.0` - Web框架
- `requests==2.31.0` - HTTP請求庫

## 🧪 測試端點

部署成功後，你可以測試以下端點：

### 1. 主頁
```
GET https://your-app.vercel.app/
```
**期望回應**:
```json
{
  "message": "Trading Agents Crypto - Vercel Demo",
  "status": "running",
  "environment": "vercel",
  "version": "1.0.0-test"
}
```

### 2. 健康檢查
```
GET https://your-app.vercel.app/health
```
**期望回應**:
```json
{
  "status": "healthy",
  "message": "Application is running successfully on Vercel"
}
```

### 3. 應用信息
```
GET https://your-app.vercel.app/api/info
```
**期望回應**:
```json
{
  "name": "Trading Agents Crypto",
  "environment": "Vercel Serverless",
  "mode": "Demo/Test",
  "limitations": [
    "No real-time analysis (SocketIO not supported)",
    "Limited execution time (5 minutes)",
    "No persistent storage",
    "Simplified functionality"
  ]
}
```

## ✅ 成功標準

如果所有三個端點都返回正確的JSON回應，那麼Vercel部署就是成功的！

## 🔄 下一步

一旦基本部署工作正常，我們可以：

1. **逐步添加功能** - 慢慢加回必要的依賴項
2. **添加模板支持** - 重新添加HTML模板
3. **實現簡化的分析** - 添加基本的crypto分析功能
4. **優化性能** - 針對serverless環境優化

## 🚨 故障排除

### 如果端點返回500錯誤
1. 檢查Vercel logs: `vercel logs <deployment-url>`
2. 確認Python版本兼容性
3. 檢查import錯誤

### 如果端點返回404錯誤
1. 確認routes配置正確
2. 檢查函數部署狀態
3. 驗證API endpoint路徑

### 如果構建失敗
1. 檢查requirements_vercel.txt語法
2. 確認沒有循環依賴
3. 檢查文件路徑大小寫

## 📞 獲得幫助

如果測試失敗，請提供：
1. Vercel部署URL
2. 錯誤訊息截圖
3. `vercel logs` 輸出

這將幫助我們快速診斷問題！ 