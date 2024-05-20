import React, { useRef } from "react";
import { FaBars, FaTimes } from "react-icons/fa";
import "./style.css";

function Navbar() {
  const navRef = useRef();

  const showNavbar = () => {
    navRef.current.classList.toggle("responsive_nav");
  };

  return (
    <header>
      <h3 className="brand">Auto Riparazione</h3>
      <nav ref={navRef}>
        <a href="/#">Κεντρική</a>
        <a href="/#">Υπηρεσίες</a>
        <a href="/#">Το Συνεργείο μας</a>
        <a href="/#">Φωτογραφίες</a>
        <a href="/#">Επικοινωνία</a>
        <button className="nav-btn nav-close-btn" onClick={showNavbar}>
          <FaTimes />
        </button>
      </nav>
      <button className="nav-btn" onClick={showNavbar}>
        <FaBars />
      </button>
    </header>
  );
}

export default Navbar;
