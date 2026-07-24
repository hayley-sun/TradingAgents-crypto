# Vercel 部署指南

這個指南將幫助你將 Trading Agents Crypto 部署到 Vercel 平台。

## ⚠️ 重要限制

由於 Vercel 是 serverless 平台，以下功能會受到限制：

1. **實時通信**: SocketIO 功能不可用，無法提供實時更新
2. **執行時間**: 最大執行時間 300 秒（5分鐘）
3. **記憶體**: 有限的 RAM 和存儲空間
4. **狀態持久化**: 無法在請求間保持狀態

## 📋 部署前準備

### 1. 確保文件結構
```
TradingAgents-main/
├── api/
│   └── index.py          # Vercel 入口點
├── templates/            # HTML 模板
├── assets/              # 靜態資源
├── tradingagents/       # 主要代碼
├── vercel.json          # Vercel 配置
├── web_app_vercel.py    # 簡化版應用
├── requirements_vercel.txt  # 依賴項
└── VERCEL_DEPLOYMENT.md # 此文件
```

### 2. 環境變量設置

在 Vercel 項目設置中添加以下環境變量：

```
PYTHONPATH=.
FLASK_ENV=production
```

## 🚀 部署步驟

### 方法 1: 通過 Vercel CLI

1. **安裝 Vercel CLI**
   ```bash
   npm install -g vercel
   ```

2. **登錄 Vercel**
   ```bash
   vercel login
   ```

3. **部署項目**
   ```bash
   vercel --prod
   ```

### 方法 2: 通過 GitHub 集成

1. **推送代碼到 GitHub**
   ```bash
   git add .
   git commit -m "Add Vercel deployment configuration"
   git push origin main
   ```

2. **連接到 Vercel**
   - 訪問 [Vercel Dashboard](https://vercel.com/dashboard)
   - 點擊 "New Project"
   - 選擇你的 GitHub 倉庫
   - 配置構建設置（通常自動檢測）

3. **部署**
   - Vercel 會自動開始部署
   - 等待部署完成

## ⚙️ 配置說明

### vercel.json 配置
```json
{
  "version": 2,
  "builds": [
    {
      "src": "api/index.py",
      "use": "@vercel/python",
      "config": {
        "maxLambdaSize": "50mb"
      }
    },
    {
      "src": "templates/**",
      "use": "@vercel/static"
    },
    {
      "src": "assets/**", 
      "use": "@vercel/static"
    }
  ],
  "routes": [
    {
      "src": "/assets/(.*)",
      "dest": "/assets/$1"
    },
    {
      "src": "/(.*)",
      "dest": "/api/index.py"
    }
  ],
  "env": {
    "PYTHONPATH": "."
  },
  "functions": {
    "api/index.py": {
      "maxDuration": 300
    }
  }
}
```

### 功能差異

| 功能 | 本地版本 | Vercel 版本 |
|------|----------|-------------|
| 實時更新 | ✅ SocketIO | ❌ 不支持 |
| 長時間分析 | ✅ 背景執行 | ❌ 5分鐘限制 |
| 多用戶並發 | ✅ 支持 | ⚠️ 有限支持 |
| 狀態持久化 | ✅ 記憶體中 | ❌ 請求間不保持 |

## 🛠️ 故障排除

### 常見問題

1. **依賴項過大**
   ```
   Error: Lambda size exceeds limit
   ```
   **解決方案**: 減少 `requirements_vercel.txt` 中的依賴項

2. **執行超時**
   ```
   Error: Function execution timed out
   ```
   **解決方案**: 優化代碼或使用更快的 LLM 模型

3. **模板找不到**
   ```
   TemplateNotFound: index.html
   ```
   **解決方案**: 確保 `templates/` 文件夾已正確上傳

### 調試技巧

1. **查看日誌**
   ```bash
   vercel logs <deployment-url>
   ```

2. **本地測試**
   ```bash
   vercel dev
   ```

## 🔧 優化建議

### 性能優化

1. **減少依賴項**
   - 只安裝必要的包
   - 考慮使用輕量級替代品

2. **優化分析流程**
   - 減少 LLM 調用次數
   - 使用更快的模型進行初步分析

3. **緩存策略**
   - 實現簡單的結果緩存
   - 避免重複計算

### 替代方案

如果 Vercel 限制太多，考慮以下替代方案：

1. **Railway**: 支持長時間運行的應用
2. **Render**: 提供更多的執行時間
3. **Heroku**: 傳統的 PaaS 平台
4. **DigitalOcean App Platform**: 靈活的部署選項

## 📞 獲取幫助

如果遇到部署問題：

1. 檢查 [Vercel 文檔](https://vercel.com/docs)
2. 查看項目的 GitHub Issues
3. 聯繫項目維護者

## 🎉 部署成功

部署成功後，你的應用將可以通過 Vercel 提供的 URL 訪問。記住，這是一個簡化版本，主要用於演示和輕量級使用。

對於生產環境和完整功能，建議使用支持長時間運行的平台部署原始版本。 