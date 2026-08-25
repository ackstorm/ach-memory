const fs = require("fs");
const path = require("path");

module.exports = async function () {
  let activation;
  try {
    activation = fs.readFileSync(path.join(__dirname, "ach-memory", "activation.txt"), "utf8").trim();
  } catch {
    return {};
  }
  if (!activation) return {};

  return {
    "experimental.chat.system.transform": async (_input, output) => {
      if (Array.isArray(output?.system) && !output.system.includes(activation)) output.system.push(activation);
    },
  };
};
