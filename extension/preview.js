/**
 * Preview Adapter Script:
 * Installs an in-memory synthetic Chrome API environment before dashboard.js executes.
 * Eliminates duplicate business/rendering logic: production common-client.js and dashboard.js
 * execute directly on top of this synthetic adapter.
 * STRICT: ZERO connection to Native Messaging, ZERO network calls.
 */

(() => {
  // Guard against real network calls
  window.fetch = () => {
    throw new Error("VIOLATION: 网络请求在离线演示模式中被严格禁止！");
  };
  window.XMLHttpRequest = function () {
    throw new Error("VIOLATION: XMLHttpRequest 在离线演示模式中被严格禁止！");
  };

  // Synthetic Fixtures State (9 Comprehensive States)
  const SYNTHETIC_FIXTURES = [
    {
      job_id: "job_synth_01_running",
      course_title: "高等数学（下）",
      sub_title: "第 3 讲 · 多元函数微分学及其几何应用",
      lecturer_name: "张教授",
      room_name: "西二楼 102",
      status: "running",
      audio_ready: true,
      page_required: false,
      created_at: 1727800000000,
      progress_info: {
        stage: "transcription",
        total_chunks: 10,
        completed_chunks: 5,
        current_chunk: 6,
        chunk_percent: 50,
        processed_seconds: 900,
        total_seconds: 1800,
        elapsed_seconds: 245,
        eta_seconds: 195,
        speed: "2.3x",
        message: "正在转写第 6/10 分块...",
        last_activity: "2026-10-01 21:15:30",
      },
    },
    {
      job_id: "job_synth_02_acq",
      course_title: "概率论与数理统计",
      sub_title: "第 1 讲 · 随机事件及其概率空间",
      lecturer_name: "李教授",
      room_name: "南楼 204",
      status: "running",
      audio_ready: false,
      page_required: true,
      created_at: 1727800100000,
      progress_info: {
        stage: "acquisition",
        total_chunks: 0,
        processed_seconds: 0,
        audio_seconds: 120,
        total_seconds: 2400,
        elapsed_seconds: 35,
        eta_seconds: null,
        message: "正在从课程网页嗅探音频流...",
        last_activity: "2026-10-01 21:16:02",
      },
    },
    {
      job_id: "job_synth_03_pausing",
      course_title: "大学物理 A",
      sub_title: "第 4 讲 · 电磁感应与麦克斯韦方程组",
      lecturer_name: "陈教授",
      room_name: "物理馆 301",
      status: "pausing",
      audio_ready: true,
      page_required: false,
      created_at: 1727800200000,
      progress_info: {
        stage: "transcription",
        total_chunks: 8,
        completed_chunks: 3,
        chunk_percent: 37.5,
        processed_seconds: 600,
        total_seconds: 1600,
        elapsed_seconds: 190,
        eta_seconds: null,
        message: "正在保存已转写断点并暂停任务...",
        last_activity: "2026-10-01 21:14:10",
      },
    },
    {
      job_id: "job_synth_04_paused",
      course_title: "线性代数",
      sub_title: "第 2 讲 · 矩阵的逆与分块矩阵运算",
      lecturer_name: "赵老师",
      room_name: "北楼 105",
      status: "paused",
      audio_ready: true,
      page_required: false,
      cache_bytes: 41943040,
      created_at: 1727800300000,
      progress_info: {
        stage: "transcription",
        total_chunks: 12,
        completed_chunks: 6,
        chunk_percent: 50,
        processed_seconds: 1200,
        total_seconds: 2400,
        elapsed_seconds: 320,
        eta_seconds: null,
        message: "任务已由用户手动暂停，可点击“继续”推进",
        last_activity: "2026-10-01 21:10:00",
      },
    },
    {
      job_id: "job_synth_05_cancelling",
      course_title: "大学英语读写",
      sub_title: "Unit 3 · Critical Thinking in Science",
      lecturer_name: "Smith",
      room_name: "外语楼 202",
      status: "cancelling",
      audio_ready: false,
      page_required: false,
      created_at: 1727800400000,
      progress_info: {
        stage: "acquisition",
        message: "正在取消任务并清理未完成临时切片...",
        last_activity: "2026-10-01 21:16:40",
      },
    },
    {
      job_id: "job_synth_06_cancelled",
      course_title: "工程力学",
      sub_title: "第 1 讲 · 刚体受力分析与平衡方程",
      lecturer_name: "孙教授",
      room_name: "机械馆 101",
      status: "cancelled",
      audio_ready: false,
      page_required: false,
      cache_bytes: 20971520,
      created_at: 1727800450000,
      progress_info: {
        stage: "preparing",
        message: "任务已取消",
        last_activity: "2026-10-01 21:05:00",
      },
    },
    {
      job_id: "job_synth_07_refresh",
      course_title: "计算机系统结构",
      sub_title: "第 5 讲 · 指令系统与流水线冒险",
      lecturer_name: "钱教授",
      room_name: "嘉定智信馆 205",
      status: "needs_refresh",
      audio_ready: false,
      page_required: false,
      source_url: "https://look.tongji.edu.cn/livingroom?course_id=101&sub_id=202&tenant_code=tongji",
      created_at: 1727800500000,
      progress_info: {
        stage: "acquisition",
        message: "流媒体签名已失效（HTTP 403），请打开课程页刷新后重新提交",
        last_activity: "2026-10-01 21:12:00",
      },
    },
    {
      job_id: "job_synth_08_failed",
      course_title: "模拟电子技术基础",
      sub_title: "第 6 讲 · 差分放大电路与共模抑制比",
      lecturer_name: "孙老师",
      room_name: "电信馆 103",
      status: "failed",
      audio_ready: false,
      page_required: false,
      cache_bytes: 10485760,
      error_message: "音频解码异常：遇到无法解析的损坏采样包 (frame corrupted at 00:12:44)",
      created_at: 1727800600000,
      progress_info: {
        stage: "transcription",
        message: "转写失败",
        last_activity: "2026-10-01 21:11:30",
      },
    },
    {
      job_id: "job_synth_09_completed",
      course_title: "算法设计与分析",
      sub_title: "第 8 讲 · 动态规划与贪心策略对比",
      lecturer_name: "王教授",
      room_name: "嘉定智信馆 117",
      status: "completed",
      audio_ready: true,
      page_required: false,
      output_dir: "D:\\同济课程逐字稿\\算法设计与分析_第8讲.md",
      cache_bytes: 84934656,
      created_at: 1727800700000,
      progress_info: {
        stage: "completed",
        total_chunks: 10,
        completed_chunks: 10,
        chunk_percent: 100,
        processed_seconds: 3200,
        total_seconds: 3200,
        elapsed_seconds: 480,
        eta_seconds: 0,
        message: "转写已完成，Markdown 逐字稿与摘要已导出",
        last_activity: "2026-10-01 21:00:00",
      },
    },
    {
      job_id: "job_synth_10_queued",
      course_title: "毛泽东思想和中国特色社会主义理论体系概论",
      sub_title: "第 2 讲 · 新民主主义革命理论",
      lecturer_name: "周教授",
      room_name: "三好坞大礼堂",
      status: "queued",
      audio_ready: false,
      page_required: false,
      created_at: 1727800800000,
      progress_info: {
        stage: "preparing",
        message: "排队等待后台处理队列...",
        last_activity: "2026-10-01 21:17:00",
      },
    },
  ];

  let inMemoryJobs = JSON.parse(JSON.stringify(SYNTHETIC_FIXTURES));
  const inMemoryStorage = {
    modelSource: "preset",
    model: "small",
    customModelPath: "",
    isOffline: false,
    outputDir: "D:\\同济课程逐字稿",
    keepAudio: false,
    resourceProfile: "balanced",
    settings_initialized: true,
  };

  const syntheticOpenedTabs = [];

  // Construct Mock Chrome API
  const mockChrome = {
    runtime: {
      id: "tongji-clipper-ext-id",
      getURL(path) {
        return path || "";
      },
      sendMessage(msg, callback) {
        if (!msg || msg.type !== "NATIVE_ACTION") {
          if (callback) callback({ error: "Unsupported message type in preview" });
          return;
        }

        setTimeout(() => {
          const payload = msg.payload || {};
          const action = payload.action;

          if (action === "ping") {
            callback({ status: "ok", port: 48721, worker_pid: 9999 });
            return;
          }

          if (action === "system_info") {
            callback({
              status: "ok",
              defaults: {
                model: "small",
                offline: false,
                output_dir: "D:\\同济课程逐字稿",
                resource_profile: "balanced",
              },
              worker: { runner_alive: true, last_heartbeat: Date.now() },
              diagnostics: [{ name: "CPU 矢量指令集 (AVX2)", ok: true, message: "可用" }],
              cache: { bytes: 84934656 },
            });
            return;
          }

          if (action === "list_jobs") {
            callback({ status: "ok", jobs: JSON.parse(JSON.stringify(inMemoryJobs)) });
            return;
          }

          if (action === "pause") {
            const j = inMemoryJobs.find((x) => x.job_id === payload.job_id);
            if (j) j.status = "paused";
            callback({ status: "ok" });
            return;
          }

          if (action === "resume") {
            const j = inMemoryJobs.find((x) => x.job_id === payload.job_id);
            if (j) j.status = "running";
            callback({ status: "ok" });
            return;
          }

          if (action === "cancel") {
            const j = inMemoryJobs.find((x) => x.job_id === payload.job_id);
            if (j) j.status = "cancelled";
            callback({ status: "ok" });
            return;
          }

          if (action === "retry") {
            const j = inMemoryJobs.find((x) => x.job_id === payload.job_id);
            if (j) {
              j.status = "running";
              delete j.error_message;
            }
            callback({ status: "ok" });
            return;
          }

          if (action === "cleanup_cache") {
            const j = inMemoryJobs.find((x) => x.job_id === payload.job_id);
            const freed = (j && j.cache_bytes) || 84934656;
            if (j) j.cache_bytes = 0;
            callback({ status: "ok", freed_bytes: freed });
            return;
          }

          if (action === "export") {
            callback({
              status: "ok",
              files: ["note.md", "metadata.json"],
              output_dir: payload.output_dir || "D:\\同济课程逐字稿",
            });
            return;
          }

          if (action === "open_output") {
            callback({ status: "ok" });
            return;
          }

          if (action === "preview") {
            callback({
              status: "ok",
              source_url: "https://look.tongji.edu.cn/livingroom?course_id=101&sub_id=202&tenant_code=tongji",
              segments: [
                {
                  start: 0,
                  end: 28.5,
                  text: "同学们好，今天我们继续深入讨论动态规划与贪心算法的核心区别与适用场景。",
                  needs_review: false,
                },
                {
                  start: 28.5,
                  end: 65.2,
                  text: "动态规划的核心在于具备最优子结构性质以及重叠子问题。我们通过记忆化或者递推表格避免重复计算。",
                  needs_review: true, // Only suggests review, no fake accuracy claims
                },
                {
                  start: 65.2,
                  end: 110.0,
                  text: "而贪心选择性质要求我们在每一步所做的局部最优选择，最终能导出全局最优解。这并非对所有问题都成立。",
                  needs_review: false,
                },
                {
                  start: 110.0,
                  end: 165.4,
                  text: "下面我们来看经典的背包问题：0-1 背包问题具有重叠子问题但不具有贪心选择性质，而分数背包问题则可以通过贪心策略求解。",
                  needs_review: true,
                },
              ],
            });
            return;
          }

          if (action === "validate_config") {
            callback({
              status: "ok",
              checks: [
                {
                  name: "Whisper 识别模型环境",
                  ok: true,
                  message: `模型 '${payload.config.model}' 架构验证通过，CPU int8 量化兼容`,
                },
                {
                  name: "系统资源调度配置",
                  ok: true,
                  message: `资源档位 '${payload.config.resource_profile}' 有效，自适应线程池就绪`,
                },
                {
                  name: "逐字稿输出目录权限",
                  ok: true,
                  message: "输出目录有效且具备写入权限",
                },
                {
                  name: "运行环境网络隔离",
                  ok: true,
                  message: payload.config.offline
                    ? "严格离线模式已启用，禁用外网下载"
                    : "在线模式已启用，允许按需下载",
                },
              ],
            });
            return;
          }

          callback({ error: `未处理的合成动作: ${action}` });
        }, 10);
      },
    },
    storage: {
      local: {
        get(keys, cb) {
          const res = {};
          (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
            if (inMemoryStorage[k] !== undefined) res[k] = inMemoryStorage[k];
          });
          setTimeout(() => cb(res), 5);
        },
        set(data, cb) {
          Object.assign(inMemoryStorage, data);
          setTimeout(() => {
            if (cb) cb();
          }, 5);
        },
      },
      onChanged: {
        addListener() {},
      },
    },
    tabs: {
      create(opts) {
        syntheticOpenedTabs.push(opts.url);
        if (typeof showToast === "function") {
          showToast(`已模拟打开页面: ${opts.url}`);
        }
      },
    },
  };

  window.chrome = mockChrome;
  if (typeof globalThis !== "undefined") {
    globalThis.chrome = mockChrome;
  }

  // Expose test helper to inspect synthetic state
  const testState = {
    getJobs: () => inMemoryJobs,
    resetJobs: () => {
      inMemoryJobs = JSON.parse(JSON.stringify(SYNTHETIC_FIXTURES));
    },
    getOpenedTabs: () => syntheticOpenedTabs,
    getStorage: () => inMemoryStorage,
  };
  window.__preview_test_state__ = testState;
  if (typeof globalThis !== "undefined") {
    globalThis.__preview_test_state__ = testState;
  }
})();
