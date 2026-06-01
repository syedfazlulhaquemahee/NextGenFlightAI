(function () {
  function closeAll(except) {
    document.querySelectorAll(".portal-menu[open]").forEach(function (menu) {
      if (except && menu === except) return;
      menu.removeAttribute("open");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var menus = Array.prototype.slice.call(document.querySelectorAll(".portal-menu"));
    if (!menus.length) return;

    var desktopHover = window.matchMedia("(hover: hover) and (pointer: fine)");

    menus.forEach(function (menu) {
      var trigger = menu.querySelector("summary.portal-menu-trigger");
      if (!trigger) return;

      var closeTimer = null;
      function clearCloseTimer() {
        if (closeTimer) {
          clearTimeout(closeTimer);
          closeTimer = null;
        }
      }

      function scheduleClose() {
        clearCloseTimer();
        closeTimer = setTimeout(function () {
          menu.removeAttribute("open");
        }, 90);
      }

      trigger.addEventListener("click", function (event) {
        event.preventDefault();
        var isOpen = menu.hasAttribute("open");
        closeAll(menu);
        if (isOpen) {
          menu.removeAttribute("open");
        } else {
          menu.setAttribute("open", "");
        }
      });

      if (desktopHover.matches) {
        menu.addEventListener("mouseenter", function () {
          clearCloseTimer();
          closeAll(menu);
          menu.setAttribute("open", "");
        });

        /* Close as soon as pointer leaves circular trigger/menu zone. */
        menu.addEventListener("mouseleave", function () {
          scheduleClose();
        });
      }
    });

    document.addEventListener("click", function (event) {
      if (event.target.closest(".portal-menu")) return;
      closeAll();
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeAll();
    });
  });
})();
