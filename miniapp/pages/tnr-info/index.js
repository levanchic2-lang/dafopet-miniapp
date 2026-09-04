Page({
  data: {
    sections: [
      {
        key: "scope",
        no: "01",
        title: "申请、抓捕与放归",
        open: true,
        paragraphs: [
          "一只流浪猫需单独提交一份申请，并上传至少2张清晰照片。申请通过医院审核后，方可预约手术。",
          "医院负责术前评估、绝育手术及约定的医疗服务，不提供流浪猫抓捕、接送和放归服务。救助人需自行安全抓捕、按时送院，并在猫咪恢复后负责放归。"
        ]
      },
      {
        key: "risk",
        no: "02",
        title: "健康与手术风险",
        open: false,
        paragraphs: [
          "流浪猫的年龄、病史、疫苗史、驱虫史及传染病情况通常不明确，即使完成基础术前评估，仍可能出现麻醉异常、术中出血、潜在疾病发作、伤口感染或恢复不良等风险。",
          "具体麻醉及手术风险，需另行阅读并签署《TNR麻醉及手术知情同意书》。"
        ]
      },
      {
        key: "deworm",
        no: "03",
        title: "驱虫与住院",
        open: false,
        paragraphs: [
          "流浪猫可能携带跳蚤、蜱虫及其他寄生虫。需要住院的猫咪，应按医院要求完成必要的驱虫处理。",
          "救助人可以自带正规猫用驱虫药，由医护人员根据猫咪体重和身体状况确认后使用，请勿自行给药。",
          "没有合适术后观察场所，或猫咪需要继续恢复和治疗时，可以选择住院。"
        ]
      },
      {
        key: "suture",
        no: "04",
        title: "母猫伤口缝合",
        open: false,
        paragraphs: [
          "皮外缝合是本院TNR常用方式，便于观察伤口，通常术后5至7天返院复查，由医生判断是否拆线。",
          "皮内可吸收缝合（俗称美容缝合）通常无需拆线，适合放归后难以再次捕捉的猫咪；少数猫咪可能出现局部瘙痒、红肿、线结反应或舔咬伤口。",
          "若救助人未选择缝合方式，院方将默认采用皮外缝合。最终方式由手术医生根据猫咪身体状况、切口情况及放归安排决定。"
        ]
      },
      {
        key: "aftercare",
        no: "05",
        title: "术后观察、疫苗与放归",
        open: false,
        paragraphs: [
          "手术后可以接回安全场所观察，也可以选择住院，具体安排以猫咪恢复情况及医生评估为准。",
          "出现伤口出血、裂开、明显红肿、渗液、持续舔咬、精神异常或长时间不进食时，请及时联系医院。",
          "如恢复情况允许，救助人可在放归前向医护人员提出猫三联疫苗接种需求，是否适合接种及具体接种时间以医生评估为准。",
          "猫咪达到放归条件后，由救助人自行接走并放归。皮外缝合的猫咪应按医院通知返院复查和拆线。"
        ]
      }
    ]
  },

  toggleSection(e) {
    const key = e.currentTarget.dataset.key;
    const sections = this.data.sections.map((item) =>
      item.key === key ? Object.assign({}, item, { open: !item.open }) : item
    );
    this.setData({ sections });
  },

  goFlow() {
    wx.navigateTo({ url: "/pages/tnr-guide/index" });
  },

  goApply() {
    wx.navigateTo({ url: "/pages/apply/index" });
  },

  onShareAppMessage() {
    return { title: "流浪猫TNR服务指南｜大风动物医院", path: "/pages/tnr-info/index" };
  },

  onShareTimeline() {
    return { title: "流浪猫TNR服务指南｜大风动物医院" };
  }
});
