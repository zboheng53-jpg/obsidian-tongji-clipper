(() => {
  if (location.hostname !== "look.tongji.edu.cn" || location.pathname !== "/livingroom") {
    throw new Error("当前页面不是同济课程回放页");
  }

  const courseElement = document.querySelector(".course_name");
  const video = document.querySelector("video");
  const videoUrl = video && (video.currentSrc || video.src);
  if (!courseElement) throw new Error("未找到课程标题，请等待页面加载完成");
  if (!videoUrl) throw new Error("未找到视频地址，请等待播放器加载完成");

  const rawTitle = (courseElement.title || courseElement.textContent || "")
    .replace(/^\s*回放\s*/, "")
    .replace(/\s+/g, " ")
    .trim();
  const sessionMatch = rawTitle.match(/(\d{4}-\d{2}-\d{2}\s*第[^\s]+节)/);
  const publishedMatch = rawTitle.match(/\d{4}-\d{2}-\d{2}/);
  const session = sessionMatch ? sessionMatch[1].replace(/\s+/g, "") : "";
  const courseName = sessionMatch
    ? rawTitle.slice(0, sessionMatch.index).trim()
    : rawTitle;
  const title = [courseName, session].filter(Boolean).join(" ");

  const details = document.querySelector(".course_info_detail");
  const text = (selector) => {
    const element = details && details.querySelector(selector);
    return element ? (element.title || element.textContent || "").replace(/\s+/g, " ").trim() : "";
  };

  return {
    page_url: location.href,
    video_url: videoUrl,
    title,
    course_name: courseName,
    session,
    author: text(".teacher_span"),
    published: publishedMatch ? publishedMatch[0] : "",
    room: text(".address_span"),
    class_time: text(".time_span")
  };
})()
