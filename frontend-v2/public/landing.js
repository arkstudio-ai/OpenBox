(function () {
  const config = {
    siteUrl: "",
    ctaUrl: "https://ai.bossipai.com.cn/",
    consoleUrl: "/",
    contactUrl: "mailto:hello@bossip.ai",
    ...(window.BOSSIP_LANDING_CONFIG || {}),
  };

  const copy = {
    zh: {
      title: "BossIP | 云端 AI 经营工作台",
      description:
        "BossIP 是面向老板和经营团队的云端 AI 工作台，覆盖营销、运营、财务、企业管理、客服和经营分析。",
      "nav.capabilities": "能力",
      "nav.workflow": "工作流",
      "nav.useCases": "场景",
      "nav.faq": "FAQ",
      "nav.signIn": "登录",
      "nav.cta": "立刻开始",
      "hero.eyebrow": "给夫妻店和成长型团队的云端 AI 工作台",
      "hero.title": "把经营、运营和管理，交给一个随时在线的 AI 搭档。",
      "hero.lede":
        "BossIP 帮老板把营销、订单、客户、财务和团队管理串在一个工作空间里，从想法到执行都有清晰下一步。",
      "hero.primaryCta": "立刻开始",
      "hero.secondaryCta": "查看能力",
      "hero.metricOneValue": "全天候",
      "hero.metricOneLabel": "云端可用",
      "hero.metricTwoValue": "经营全链路",
      "hero.metricTwoLabel": "从获客到复盘",
      "preview.title": "今日经营工作台",
      "preview.status": "AI 在线",
      "preview.railPlan": "计划",
      "preview.railMarket": "营销",
      "preview.railFinance": "财务",
      "preview.railTeam": "团队",
      "preview.panelKicker": "下一步建议",
      "preview.panelTitle": "周末套餐上线前检查",
      "preview.panelTime": "今天 16:30",
      "preview.taskOneTitle": "生成三条本地生活投放文案",
      "preview.taskOneBody": "按客单价、时段和库存自动拆分卖点。",
      "preview.taskTwoTitle": "复核毛利和备货量",
      "preview.taskTwoBody": "把原料成本、预估销量和人员排班放到同一张表里。",
      "preview.taskThreeTitle": "同步客服回复口径",
      "preview.taskThreeBody": "自动整理常见问题，并标记需要人工确认的边界。",
      "preview.signalKicker": "经营信号",
      "preview.signalValue": "+18%",
      "preview.signalBody": "午市转化率较上周提升",
      "band.copy": "一个账号连接每天的经营判断、执行任务和团队协作。",
      "capabilities.eyebrow": "核心能力",
      "capabilities.title": "不只做营销，也能接住经营里的琐碎重活。",
      "capabilities.marketingTitle": "营销增长",
      "capabilities.marketingBody":
        "生成活动方案、投放文案、社媒内容和渠道复盘，让获客动作更快闭环。",
      "capabilities.operationsTitle": "门店与运营",
      "capabilities.operationsBody":
        "整理订单、库存、排班、客服和供应商事项，把日常动作拆成可执行清单。",
      "capabilities.financeTitle": "财务与报表",
      "capabilities.financeBody":
        "汇总流水、成本、毛利和现金流，用老板看得懂的方式解释关键变化。",
      "capabilities.managementTitle": "企业管理",
      "capabilities.managementBody":
        "把会议、制度、目标和项目推进变成结构化记录，帮助团队对齐责任人和截止时间。",
      "workflow.eyebrow": "工作方式",
      "workflow.title": "从一句话开始，落到可追踪的业务动作。",
      "workflow.stepOneTitle": "说清目标",
      "workflow.stepOneBody":
        "例如“下周做一个堂食复购活动”或“帮我看本月利润为什么下降”。",
      "workflow.stepTwoTitle": "拆解资料和任务",
      "workflow.stepTwoBody":
        "AI 会询问缺失信息，整理表格、文档和历史记录，形成可执行步骤。",
      "workflow.stepThreeTitle": "执行并复盘",
      "workflow.stepThreeBody":
        "把结果沉淀为模板、报表和下一轮建议，减少每次从零开始。",
      "useCases.eyebrow": "适用场景",
      "useCases.title": "老板每天会遇到的事，都可以先交给 BossIP 整理一遍。",
      "useCases.storeTitle": "夫妻店和连锁门店",
      "useCases.storeBody":
        "新品上架、团购活动、差评处理、排班备货、日报周报。",
      "useCases.brandTitle": "本地生活与品牌团队",
      "useCases.brandBody":
        "渠道内容、达人 brief、活动预算、线索跟进和投放复盘。",
      "useCases.companyTitle": "成长型公司",
      "useCases.companyBody":
        "经营分析、会议纪要、项目推进、客户方案和管理制度。",
      "faq.eyebrow": "FAQ",
      "faq.title": "开始前，老板通常会问这些。",
      "faq.oneQuestion": "BossIP 更像聊天工具还是业务系统？",
      "faq.oneAnswer":
        "它更像可以持续工作的云端业务助手：既能对话，也能围绕任务、资料和结果持续推进。",
      "faq.twoQuestion": "页面会自动切换语言吗？",
      "faq.twoAnswer":
        "会。BossIP 会优先读取浏览器语言，也支持通过右上角按钮或 URL 参数切换中文和英文。",
      "faq.threeQuestion": "CTA 和联系方式能换成正式环境吗？",
      "faq.threeAnswer":
        "可以。部署时通过运行时配置设置演示预约、登录入口、联系地址和站点 URL。",
      "final.eyebrow": "Ready when you are",
      "final.title": "让 AI 先帮你把今天的经营事项理顺。",
      "final.cta": "立刻开始",
      "footer.copy": "面向企业经营和运营的云端 AI 工作台。",
      "footer.contact": "联系团队",
    },
    en: {
      title: "BossIP | Cloud AI workspace for business operations",
      description:
        "BossIP is a cloud AI workspace for owners and business teams, covering marketing, operations, finance, management, customer service, and analytics.",
      "nav.capabilities": "Capabilities",
      "nav.workflow": "Workflow",
      "nav.useCases": "Use cases",
      "nav.faq": "FAQ",
      "nav.signIn": "Sign in",
      "nav.cta": "Start now",
      "hero.eyebrow": "Cloud AI workspace for owners and growing teams",
      "hero.title":
        "Give your daily operations an AI partner that stays online.",
      "hero.lede":
        "BossIP brings marketing, orders, customers, finance, and team management into one workspace, turning ideas into clear next steps.",
      "hero.primaryCta": "Start now",
      "hero.secondaryCta": "View capabilities",
      "hero.metricOneValue": "Always on",
      "hero.metricOneLabel": "Available in the cloud",
      "hero.metricTwoValue": "Full journey",
      "hero.metricTwoLabel": "From growth to review",
      "preview.title": "Today’s business workspace",
      "preview.status": "AI online",
      "preview.railPlan": "Plan",
      "preview.railMarket": "Market",
      "preview.railFinance": "Finance",
      "preview.railTeam": "Team",
      "preview.panelKicker": "Recommended next step",
      "preview.panelTitle": "Weekend bundle launch checklist",
      "preview.panelTime": "Today 16:30",
      "preview.taskOneTitle": "Draft three local campaign messages",
      "preview.taskOneBody":
        "Split selling points by price, time slot, and available inventory.",
      "preview.taskTwoTitle": "Review margin and stock plan",
      "preview.taskTwoBody":
        "Compare ingredient cost, expected sales, and staffing in one table.",
      "preview.taskThreeTitle": "Align customer-service replies",
      "preview.taskThreeBody":
        "Summarize common questions and flag cases that need human review.",
      "preview.signalKicker": "Business signal",
      "preview.signalValue": "+18%",
      "preview.signalBody": "Lunch conversion is up from last week",
      "band.copy":
        "One account connects daily decisions, execution tasks, and team collaboration.",
      "capabilities.eyebrow": "Core capabilities",
      "capabilities.title":
        "More than marketing: BossIP helps carry the operational weight.",
      "capabilities.marketingTitle": "Marketing growth",
      "capabilities.marketingBody":
        "Create campaign plans, ad copy, social content, and channel reviews so growth work closes faster.",
      "capabilities.operationsTitle": "Store and operations",
      "capabilities.operationsBody":
        "Organize orders, inventory, shifts, customer service, and supplier work into clear action lists.",
      "capabilities.financeTitle": "Finance and reporting",
      "capabilities.financeBody":
        "Summarize revenue, cost, margin, and cash flow, then explain key changes in plain business language.",
      "capabilities.managementTitle": "Business management",
      "capabilities.managementBody":
        "Turn meetings, policies, goals, and projects into structured records with owners and deadlines.",
      "workflow.eyebrow": "How it works",
      "workflow.title":
        "Start with one request. End with tracked business actions.",
      "workflow.stepOneTitle": "Name the goal",
      "workflow.stepOneBody":
        "For example, “plan next week’s repeat-purchase offer” or “explain why profit fell this month.”",
      "workflow.stepTwoTitle": "Break down inputs and tasks",
      "workflow.stepTwoBody":
        "The AI asks for missing context, organizes files and records, then turns them into executable steps.",
      "workflow.stepThreeTitle": "Execute and review",
      "workflow.stepThreeBody":
        "Results become reusable templates, reports, and next-round suggestions, reducing repeated setup work.",
      "useCases.eyebrow": "Use cases",
      "useCases.title": "Let BossIP organize the work owners face every day.",
      "useCases.storeTitle": "Owner-operated and multi-store teams",
      "useCases.storeBody":
        "New products, local deals, review handling, shift planning, stock prep, and weekly reports.",
      "useCases.brandTitle": "Local commerce and brand teams",
      "useCases.brandBody":
        "Channel content, creator briefs, campaign budgets, lead follow-up, and performance reviews.",
      "useCases.companyTitle": "Growing companies",
      "useCases.companyBody":
        "Business analysis, meeting notes, project follow-through, client proposals, and management policies.",
      "faq.eyebrow": "FAQ",
      "faq.title": "Questions business owners usually ask first.",
      "faq.oneQuestion": "Is BossIP a chat tool or a business system?",
      "faq.oneAnswer":
        "It is closer to a cloud business assistant that keeps working: you can chat, but it also tracks tasks, source material, and outcomes.",
      "faq.twoQuestion": "Does the page switch language automatically?",
      "faq.twoAnswer":
        "Yes. BossIP reads the browser language first, and you can also switch with the button or a URL parameter.",
      "faq.threeQuestion":
        "Can CTA links and contact details use production values?",
      "faq.threeAnswer":
        "Yes. Set the demo link, sign-in link, contact URL, and site URL through runtime configuration during deployment.",
      "final.eyebrow": "Ready when you are",
      "final.title":
        "Let AI organize today’s operating work before it piles up.",
      "final.cta": "Start now",
      "footer.copy": "Cloud AI workspace for business operations.",
      "footer.contact": "Contact",
    },
  };

  const params = new URLSearchParams(window.location.search);
  const requestedLanguage = params.get("lang");
  const storedLanguage = window.localStorage.getItem("bossip-landing-lang");
  const browserLanguage = navigator.language || "";
  const initialLanguage =
    requestedLanguage === "en" || requestedLanguage === "zh"
      ? requestedLanguage
      : storedLanguage === "en" || storedLanguage === "zh"
        ? storedLanguage
        : browserLanguage.toLowerCase().startsWith("zh")
          ? "zh"
          : "en";

  function setMeta(name, value) {
    const escapedValue = String(value);
    const meta =
      document.querySelector(`meta[name="${name}"]`) ||
      document.querySelector(`meta[property="${name}"]`);
    if (meta) {
      meta.setAttribute("content", escapedValue);
    }
  }

  function absoluteUrl(pathOrUrl) {
    try {
      return new URL(
        pathOrUrl,
        config.siteUrl || window.location.origin,
      ).toString();
    } catch {
      return pathOrUrl;
    }
  }

  function applyConfig() {
    document.querySelectorAll("[data-config-link]").forEach((element) => {
      const key = element.getAttribute("data-config-link");
      const value = key ? config[key] : "";
      if (value) {
        element.setAttribute("href", value);
      }
    });

    const canonical = document.querySelector("[data-bossip-canonical]");
    if (canonical) {
      canonical.setAttribute("href", absoluteUrl("/landing"));
    }
    setMeta("og:image", absoluteUrl("./bossip-app-icon.svg"));
  }

  function applyLanguage(language) {
    const dictionary = copy[language] || copy.en;
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.body.dataset.lang = language;
    document.title = dictionary.title;
    setMeta("description", dictionary.description);
    setMeta("og:title", dictionary.title);
    setMeta("og:description", dictionary.description);
    setMeta("twitter:title", dictionary.title);
    setMeta("twitter:description", dictionary.description);

    document.querySelectorAll("[data-i18n]").forEach((element) => {
      const key = element.getAttribute("data-i18n");
      if (key && dictionary[key]) {
        element.textContent = dictionary[key];
      }
    });

    const toggle = document.querySelector("[data-language-toggle]");
    if (toggle) {
      toggle.textContent = language === "zh" ? "EN" : "中文";
      toggle.setAttribute(
        "aria-label",
        language === "zh" ? "Switch to English" : "切换到中文",
      );
    }

    window.localStorage.setItem("bossip-landing-lang", language);
    const structuredData = document.querySelector("[data-structured-data]");
    if (structuredData) {
      structuredData.textContent = JSON.stringify(
        {
          "@context": "https://schema.org",
          "@type": "SoftwareApplication",
          name: "BossIP",
          applicationCategory: "BusinessApplication",
          operatingSystem: "Web",
          description: dictionary.description,
          url: absoluteUrl("/landing"),
          offers: {
            "@type": "Offer",
            price: "0",
            priceCurrency: "USD",
          },
        },
        null,
        2,
      );
    }
  }

  applyConfig();
  applyLanguage(initialLanguage);

  document
    .querySelector("[data-language-toggle]")
    ?.addEventListener("click", () => {
      const nextLanguage = document.body.dataset.lang === "zh" ? "en" : "zh";
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("lang", nextLanguage);
      window.history.replaceState({}, "", nextUrl);
      applyLanguage(nextLanguage);
    });
})();
